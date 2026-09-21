"""Associate one org-owned Telnyx local number with a Telnyx 10DLC campaign.

NOT ROUTED. Nothing calls this service today: there is no route, job or sweeper that
imports it, and it is written as the transport-and-state-machine layer a future operator
action would drive, exactly like ``services/telnyx_campaign_filing.py``. Do not assume an
HTTP surface exists for it.

Each invocation makes one signed carrier read (the campaign lookup) plus, only if that read
confirms the campaign is approved at Telnyx, exactly one carrier write
(``POST /10dlc/phone_number_campaigns``).

Attaching a number to a campaign is the one 10DLC step the local state machine cannot
undo: a timed-out POST may already have assigned the number at Telnyx. So this module is
built around two rules - refuse anything that would assign a number to a campaign Telnyx
has not accepted, and never issue the same assignment twice for one number.

Safety rules, in the order they bind:

* The number and its campaign are re-read under row locks (``SELECT ... FOR UPDATE``)
  before anything else; the caller's in-memory objects are never trusted for carrier,
  status, ``carrier_refs`` or ``campaign_id``. The number is locked first and the campaign
  second, always in that order (see ``_locked_number``), so concurrent associations cannot
  deadlock.
* The number and the campaign must belong to the SAME org, and the number must be a
  Telnyx local number that is active. A toll-free number (which uses toll-free
  verification, not 10DLC) or a non-Telnyx number is refused.
* The campaign must be locally ``approved`` AND carry a Telnyx campaign id in
  ``carrier_refs`` - a locally approved campaign with no carrier reference is refused,
  since the number would otherwise be assigned to a campaign Telnyx has never seen.
* The carrier's own verdict is authoritative and consulted before the POST: the campaign
  GET must map to ``approved`` via ``registration_status.map_campaign_status``. An
  unreachable, malformed or ambiguous campaign lookup is fatal - we do NOT fall back to
  local approval and we do NOT spend the assignment.
* After the POST, the carrier's echoed phone number is BOUND to the requested E.164 before
  ``campaign_id`` is set. The transport only proves that *some* phone identifier is present
  (``_require_id(..., ("phoneNumber", "phone_number"), ...)``), so a 2xx that names a
  DIFFERENT number must never be recorded as this number's association.
* Idempotency is durable. After the campaign check passes, the number records an
  assignment attempt in ``OrgNumber.provisioning`` - the existing P37 per-number assignment
  JSON (``Mapped[dict]``, migration 0042, documented as messaging profile / voice
  connection / LiveKit trunk / 10DLC campaign state) - and that row is COMMITTED before the
  POST, so an in-flight, failed or timed-out assignment leaves a marker that blocks a retry
  until a human reconciles it. No DB column is added.
* ``OrgNumber.campaign_id`` is set only after a confirmed assignment response whose echoed
  number matches. An error, a timeout, a mismatched number or an unreadable response
  leaves the attempt marker in place, ``campaign_id`` NULL, and whatever local status the
  number already had.
* No secret, request body, phone number or carrier response body is ever logged; only the
  local number UUID, the local campaign UUID and fixed status strings are.

Concurrency scope - read this before assuming this is concurrency-safe everywhere
-------------------------------------------------------------------------------
``with_for_update`` is a REAL row lock on Postgres, and that is what serialises two
concurrent associations of the same number. SQLite has no ``FOR UPDATE`` and SQLAlchemy
silently makes it a no-op, so under SQLite the ``SELECT`` below takes NO lock: two
concurrent callers could both read an unset marker and both POST. This service therefore
does NOT provide row-lock concurrency safety on SQLite, and callers/tests must NOT assume
it does. Concurrency safety is a Postgres guarantee; the durable marker still makes a
later retry refuse, but it cannot prevent a simultaneous in-flight pair on SQLite.

The HTTP client is injected (``httpx.MockTransport`` in tests) exactly like
``TelnyxRegistrationClient`` and ``provider_accounts.probe_account``, so a test run never
reaches the live carrier.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import (
    ConflictError,
    FeatureUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.messaging import OrgNumber
from app.models.numbers import Campaign
from app.providers.telnyx import registration_status
from app.providers.telnyx.registration import TelnyxRegistrationClient
from app.services import provider_accounts

log = structlog.get_logger("registration.telnyx_number_association")

#: The provider this module assigns with - also the Campaign ``carrier_refs`` key.
PROVIDER = "telnyx"
#: ``OrgNumber.provisioning`` key holding the durable assignment attempt/audit marker. Its
#: mere presence blocks a repeat assignment; reconciliation means a human clears it once
#: the carrier side is understood.
ATTEMPT_KEY = "telnyx_campaign_assignment"

#: Marker state values. ``unconfirmed`` is committed BEFORE the POST and means "an
#: assignment may be in flight or may already have been accepted - carrier outcome
#: unknown". ``assigned`` is written only after a confirmed response. Any marker value
#: blocks a retry.
MARKER_STATE_UNCONFIRMED = "unconfirmed"
MARKER_STATE_ASSIGNED = "assigned"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refs(campaign: Campaign) -> dict:
    """A mutable copy of a campaign's ``carrier_refs`` - always reassigned, never mutated
    in place (``PortableJSON`` does not track in-place mutation)."""
    raw = campaign.carrier_refs
    return dict(raw) if isinstance(raw, Mapping) else {}


def _provisioning(number: OrgNumber) -> dict:
    """A mutable copy of a number's ``provisioning`` - reassigned, never mutated in place."""
    raw = number.provisioning
    return dict(raw) if isinstance(raw, Mapping) else {}


async def _locked_number(session: AsyncSession, number_id: uuid.UUID) -> OrgNumber:
    """The number row, locked for the rest of this transaction and re-populated from the
    database so a stale caller-supplied instance cannot be read by mistake.

    This is the first lock this module ever takes; the campaign lock always follows, so
    the (number, campaign) order is fixed and two concurrent associations cannot deadlock.
    ``with_for_update`` is a REAL row lock on Postgres (the production backend); on SQLite
    it is a silent no-op, so it serialises nothing there - see the module docstring.
    """
    number = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.id == number_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if number is None:
        raise NotFoundError("Number not found")
    return number


async def _locked_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    """The campaign row, locked after the number (see ``_locked_number``) and re-populated
    from the database, so a stale caller object cannot supply the carrier id or status."""
    campaign = (
        await session.execute(
            sa.select(Campaign)
            .where(Campaign.id == campaign_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise NotFoundError("Campaign not found")
    return campaign


def _require_same_org(number: OrgNumber, campaign: Campaign) -> None:
    """A number may only be assigned to a campaign of its own org.

    Deliberately reported as "not found": a cross-org pairing must not confirm that the
    other org's number or campaign exists.
    """
    if number.org_id != campaign.org_id:
        raise NotFoundError("Number or campaign not found")


def _require_assignable_number(number: OrgNumber) -> None:
    """Only an active Telnyx LOCAL number may be assigned to a 10DLC campaign.

    Toll-free numbers follow toll-free verification instead and are not interchangeable
    with 10DLC (see ``OrgNumber.number_type``).
    """
    if (number.carrier or "").strip().lower() != PROVIDER:
        raise ValidationFailedError(
            "Only a Telnyx number can be associated with a Telnyx 10DLC campaign"
        )
    if (number.number_type or "local") != "local":
        raise ValidationFailedError(
            "Only a local number can be associated with a 10DLC campaign; toll-free "
            "numbers use toll-free verification instead"
        )
    if (number.status or "") != "active" or number.is_active is False:
        raise ValidationFailedError(
            "Only an active number can be associated with a 10DLC campaign"
        )


def _require_approved_campaign_ref(campaign: Campaign) -> str:
    """The campaign's Telnyx id, which requires a locally ``approved`` campaign that
    carries a Telnyx campaign id in ``carrier_refs``.

    Local approval is necessary but NOT sufficient - the carrier verdict is checked
    separately by ``_require_campaign_approved_at_telnyx``.
    """
    status = campaign.status or "draft"
    if status != "approved":
        raise ConflictError(
            "Only an approved 10DLC campaign can have a number associated with it; this "
            f"campaign is {status!r} - reconcile carrier_refs with Telnyx before retrying"
        )
    raw = _refs(campaign).get(PROVIDER)
    carrier_campaign_id = raw.strip() if isinstance(raw, str) else ""
    if not carrier_campaign_id:
        raise ValidationFailedError(
            "The campaign is approved locally but has no Telnyx campaign id; file and "
            "approve the campaign with Telnyx before associating a number with it"
        )
    return carrier_campaign_id


def _require_unassigned(number: OrgNumber) -> None:
    """Refuse a repeat assignment: an existing ``campaign_id`` or a prior attempt marker
    both mean the only safe next step is a human reconciling the number's provisioning
    state with the carrier."""
    if number.campaign_id is not None:
        raise ConflictError(
            "This number is already associated with a 10DLC campaign; reconcile the "
            "number's campaign association before associating it again"
        )
    if _provisioning(number).get(ATTEMPT_KEY):
        raise ConflictError(
            "A Telnyx campaign association for this number was already attempted and may "
            "have been accepted; reconcile the number's provisioning state with Telnyx "
            "before retrying"
        )


def _require_attempt_is_ours(number: OrgNumber, *, campaign_id: uuid.UUID) -> None:
    """Post-POST lock check: the marker we committed must still be the live one."""
    marker = _provisioning(number).get(ATTEMPT_KEY)
    if not isinstance(marker, Mapping) or marker.get("campaign_id") != str(campaign_id):
        raise ConflictError(
            "The Telnyx assignment marker for this number changed while the assignment "
            "was in flight; reconcile the number's provisioning state with Telnyx"
        )


def _record_attempt(
    number: OrgNumber, *, campaign_id: uuid.UUID, carrier_campaign_id: str
) -> None:
    """The pre-POST marker. It is committed BEFORE the carrier call and already states that
    the carrier outcome is unknown, so a timeout or a crash leaves an accurate record
    without any further write: nothing here removes or overwrites this marker on failure."""
    provisioning = _provisioning(number)
    provisioning[ATTEMPT_KEY] = {
        "state": MARKER_STATE_UNCONFIRMED,
        "carrier_outcome": "unknown",
        "campaign_id": str(campaign_id),
        "carrier_campaign_id": carrier_campaign_id,
        "attempted_at": _now_iso(),
    }
    number.provisioning = provisioning


def _record_assigned(
    number: OrgNumber, *, campaign_id: uuid.UUID, carrier_campaign_id: str
) -> None:
    provisioning = _provisioning(number)
    marker = provisioning.get(ATTEMPT_KEY)
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    marker.update(
        {
            "state": MARKER_STATE_ASSIGNED,
            "carrier_outcome": "accepted",
            "campaign_id": str(campaign_id),
            "carrier_campaign_id": carrier_campaign_id,
            "assigned_at": _now_iso(),
        }
    )
    provisioning[ATTEMPT_KEY] = marker
    number.provisioning = provisioning


async def _telnyx_carrier_settings(session: AsyncSession, settings: Settings) -> object:
    """The credentials to assign with: the org's ACTIVE Telnyx provider account when one
    exists, otherwise the app's own env-level Telnyx config - and only when it is live.

    Both paths are the same pair the rest of the carrier stack supports
    (config.py::carrier_live / app/providers/registry_org.py); neither is used unless it
    actually yields a usable Telnyx API key - see _api_key_from.
    """
    account = await provider_accounts.active_account_for(session, PROVIDER)
    if account is not None:
        return provider_accounts.settings_like_for(settings, account)
    if settings.carrier_live(PROVIDER):
        return settings
    raise FeatureUnavailableError(
        "No active Telnyx provider account for this org; add and probe one before "
        "associating a number with a campaign"
    )


def _api_key_from(carrier_settings: object) -> str:
    raw = getattr(carrier_settings, "telnyx_api_key", None)
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    api_key = raw.strip() if isinstance(raw, str) else ""
    if not api_key:
        raise FeatureUnavailableError(
            "Telnyx API credentials are not configured for this org"
        )
    return api_key


async def _require_campaign_approved_at_telnyx(
    reg_client: TelnyxRegistrationClient, carrier_campaign_id: str, *, number_ref: str
) -> None:
    """Fail-closed carrier-side campaign gate.

    A locally approved campaign may have been marked approved by an operator rather than
    by Telnyx, and Telnyx can fail a campaign after (or between) local approvals - either
    way a number assigned to it just burns a carrier assignment. So this runs before the
    attempt marker and before any POST, and every non-approval outcome is fatal:

    * the lookup raising (timeout, transport error, unreadable response, missing campaign
      id) propagates untouched - no fallback to local approval, no POST;
    * a campaign Telnyx reports as anything other than ``approved`` (via
      ``map_campaign_status``) is refused.
    """
    carrier_campaign = await reg_client.get_campaign(carrier_campaign_id)
    if (
        registration_status.map_campaign_status(carrier_campaign)
        != registration_status.APPROVED
    ):
        log.warning("telnyx_number_association_campaign_not_approved", number_id=number_ref)
        raise ValidationFailedError(
            "Telnyx does not report the campaign as approved; resolve the campaign's "
            "Telnyx status before associating a number with it"
        )


def _returned_phone(data: Mapping) -> str:
    """The phone number Telnyx echoed in a 2xx assignment body, or "" when absent.

    The transport only guarantees that SOME phone identifier is present
    (``phoneNumber`` or ``phone_number``); it never checks what the number IS.
    Deliberately not ``str(None)``: only a non-empty string can be compared.
    """
    raw = data.get("phoneNumber")
    if raw is None:
        raw = data.get("phone_number")
    return raw.strip() if isinstance(raw, str) else ""


async def associate_number_with_telnyx(
    session: AsyncSession,
    settings: Settings,
    number: OrgNumber,
    campaign: Campaign,
    *,
    client: httpx.AsyncClient | None = None,
) -> OrgNumber:
    """Associate one local ``number`` with ``campaign`` at Telnyx.

    NOT ROUTED: no route/job calls this yet; it is invoked directly by a future operator
    action (and by tests).

    Returns the number with ``campaign_id`` set and ``provisioning`` updated. Raises
    ConflictError when the campaign is not approved or an assignment already exists,
    ValidationFailedError when local state (carrier, number type, number status), the
    carrier's campaign verdict, or the carrier's echoed phone number is not usable,
    NotFoundError when the number or campaign does not exist or they belong to different
    orgs, and FeatureUnavailableError when credentials or the carrier are not usable. No
    exception path sets ``campaign_id`` or removes the committed attempt marker.

    Concurrency safety depends on the real ``FOR UPDATE`` row lock Postgres provides; it is
    a no-op on SQLite (see the module docstring).

    ``client`` is an optional ``httpx.AsyncClient`` whose transport is injected by tests;
    when omitted this call owns (and closes) its own client.
    """
    if number.id is None:
        raise ValidationFailedError("Save the number before associating it with a campaign")
    if campaign.id is None:
        raise ValidationFailedError("Save the campaign before associating a number with it")
    number_id = number.id
    campaign_id = campaign.id

    # Row locks + authoritative re-read, before any carrier byte. The number is locked
    # first (it is the contended row - two concurrent associations of the SAME number must
    # serialise here) and the campaign second; this fixed order is used everywhere in this
    # module, so concurrent associations cannot deadlock. On SQLite the lock is a no-op.
    locked_number = await _locked_number(session, number_id)
    locked_campaign = await _locked_campaign(session, campaign_id)

    _require_same_org(locked_number, locked_campaign)
    _require_assignable_number(locked_number)
    carrier_campaign_id = _require_approved_campaign_ref(locked_campaign)
    _require_unassigned(locked_number)

    # Everything local and cheap fails before any credential is decrypted.
    carrier_settings = await _telnyx_carrier_settings(session, settings)
    api_key = _api_key_from(carrier_settings)
    phone_number = locked_number.e164

    reg_client = TelnyxRegistrationClient(api_key=api_key, client=client)
    try:
        # Fail-closed carrier check BEFORE the marker and BEFORE the POST: a timeout or an
        # ambiguous/incomplete response raises here, so nothing is committed and no
        # assignment is spent.
        await _require_campaign_approved_at_telnyx(
            reg_client, carrier_campaign_id, number_ref=str(number_id)
        )

        # The attempt marker is made durable BEFORE the POST, because a timed-out
        # phone_number_campaigns call may still have assigned the number. From this point
        # on the local record says "an attempt exists and its carrier outcome is unknown"
        # until a human reconciles it. ``campaign_id`` is deliberately NOT touched yet.
        _record_attempt(
            locked_number,
            campaign_id=campaign_id,
            carrier_campaign_id=carrier_campaign_id,
        )
        await session.commit()  # releases the row locks with the marker durable
        log.info(
            "telnyx_number_association_attempted",
            number_id=str(number_id),
            campaign_id=str(campaign_id),
        )

        try:
            data = await reg_client.assign_phone_number(
                {"phoneNumber": phone_number, "campaignId": carrier_campaign_id}
            )
        except (FeatureUnavailableError, ValidationFailedError):
            # The marker stays committed with carrier_outcome=unknown and campaign_id
            # stays NULL: the carrier may have assigned the number even though we cannot
            # tell.
            log.warning(
                "telnyx_number_association_unconfirmed",
                number_id=str(number_id),
                campaign_id=str(campaign_id),
            )
            raise

        # Bind the carrier's echo to the number we asked about. The transport proves only
        # that some phone identifier is present, so a 2xx naming a DIFFERENT number must
        # never be recorded as this number's association: keep the marker (carrier outcome
        # for THIS number stays unknown) and make a human reconcile it.
        if _returned_phone(data) != phone_number:
            log.warning(
                "telnyx_number_association_phone_mismatch", number_id=str(number_id)
            )
            raise ValidationFailedError(
                "Telnyx confirmed a phone-number assignment that does not match the "
                "requested number; reconcile the number's provisioning state with Telnyx"
            )

        # Re-lock and re-read: the earlier commit released the row, and the marker written
        # under it must still be the live one before we record the association.
        locked_number = await _locked_number(session, number_id)
        _require_attempt_is_ours(locked_number, campaign_id=campaign_id)
        _record_assigned(
            locked_number,
            campaign_id=campaign_id,
            carrier_campaign_id=carrier_campaign_id,
        )
        # The number points at the campaign only now that the carrier has confirmed it.
        locked_number.campaign_id = campaign_id
        await session.commit()
        log.info(
            "telnyx_number_association_assigned",
            number_id=str(number_id),
            campaign_id=str(campaign_id),
        )
        return locked_number
    finally:
        await reg_client.aclose()
