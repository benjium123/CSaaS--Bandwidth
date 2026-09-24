"""File one local 10DLC campaign with Telnyx - an explicit, operator-driven action.

This is a FUTURE operator action, not a live path: nothing calls it from a route yet, and
each invocation makes one signed carrier read (the brand lookup) plus, only if that read
confirms the brand, exactly one non-refundable carrier write
(``POST /10dlc/campaignBuilder``).

Filing is the one registration step the local state machine cannot undo: a timed-out POST
may already have created a campaign at Telnyx. So this module is built around two rules -
refuse anything that would file against a brand Telnyx has not approved, and never issue
the same filing twice for one campaign.

Safety rules, in the order they bind:

* The campaign is re-read under a row lock (``SELECT ... FOR UPDATE``) before anything
  else; the caller's in-memory object is never trusted for status or ``carrier_refs``.
* A campaign may be filed when it is locally ``draft`` OR locally ``submitted``, and only
  while it carries neither a Telnyx campaign id nor a prior filing marker. ``approved``,
  ``rejected`` and any unknown status are refused, as is any repeat marker or carrier id.
* The parent Brand must be approved locally AND carry a Telnyx brand id in
  ``carrier_refs`` - a locally approved brand with no carrier reference is refused, since
  the campaign would otherwise be filed against a brand Telnyx has never seen.
* The carrier's own verdict is authoritative and consulted before the POST: the brand GET
  must map to ``approved`` via ``registration_status.map_brand_status`` (which needs a
  verified identity, not merely a locally approved row). An unreachable, malformed or
  ambiguous brand lookup is fatal - we do NOT fall back to local approval and we do NOT
  spend the submission.
* Idempotency is durable. After the brand check passes, the campaign records a filing
  attempt in ``carrier_refs`` and that row is COMMITTED before the POST, so an in-flight,
  failed or timed-out filing leaves a marker that blocks a retry until a human reconciles
  it. The Telnyx ``referenceId`` carries the local campaign UUID as a second, weaker
  signal - Telnyx does not promise it is unique, so it never replaces the marker.
* Only a 2xx whose campaign id is a non-empty string records the id and advances the local
  campaign to ``submitted``. An error, a timeout, or a 2xx we cannot read leaves the
  attempt marker in place and the campaign neither approved nor submitted.
* No secret, request body, message text or carrier id is ever logged; only the local
  campaign UUID and fixed status strings are.

The HTTP client is injected (``httpx.MockTransport`` in tests) exactly like
``TelnyxRegistrationClient`` and ``provider_accounts.probe_account``, so a test run never
reaches the live carrier.

Semantic compromise - local ``submitted`` is a ready-to-file marker, NOT a carrier claim
------------------------------------------------------------------------------
The legacy local-only action ``POST /campaigns/{id}/submit`` sets ``Campaign.status`` to
``submitted`` *before* any Telnyx call: it means "ready to file", and for a Telnyx-backed
campaign it is expected to be followed by exactly this service. Refusing ``submitted``
outright would therefore block the existing operator workflow, so this service accepts a
locally ``submitted`` campaign - but only while it has no Telnyx campaign id and no prior
filing marker, i.e. only while a Telnyx submission demonstrably has not happened.

Consequently a local ``submitted`` status here is explicitly NOT a statement about the
carrier. The only status that PERMITS SENDING remains ``approved``
(``app.models.numbers.can_send``); a locally ``submitted`` campaign still cannot send, so
this compromise cannot leak an unfiled or in-flight campaign into production traffic. The
carrier's real outcome is tracked separately in the ``carrier_refs`` marker (see
``ATTEMPT_KEY``), and a timed-out filing leaves the local status exactly as it was - a
campaign that entered as ``submitted`` stays ``submitted`` - with the marker recording the
carrier outcome as unknown.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
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
from app.models.numbers import Brand, Campaign
from app.providers.telnyx import registration_status
from app.providers.telnyx.campaign_payload import build_campaign_payload
from app.providers.telnyx.registration import TelnyxRegistrationClient
from app.services import provider_accounts, registration

log = structlog.get_logger("registration.telnyx_filing")

#: The provider this module files with - also the Brand/Campaign ``carrier_refs`` key.
PROVIDER = "telnyx"
#: Campaign ``carrier_refs`` key holding the local filing attempt/audit marker. Its mere
#: presence blocks a repeat filing; reconciliation means a human clears it (and the
#: carrier id) once the carrier side is understood.
ATTEMPT_KEY = "telnyx_filing"
#: Telnyx's optional caller-supplied reference on a campaign. Best-effort de-duplication
#: only - Telnyx does not promise uniqueness, so this NEVER replaces ATTEMPT_KEY.
REFERENCE_ID_FIELD = "referenceId"

#: Local statuses from which a Telnyx filing may be attempted. ``draft`` is the normal
#: case; ``submitted`` is admitted because the legacy local-only ready-to-file route sets
#: it before any Telnyx call (see the module docstring). Both are additionally gated on
#: there being no Telnyx campaign id and no prior filing marker, so admitting ``submitted``
#: cannot re-file a campaign that a Telnyx submission has already touched.
_FILEABLE_STATUSES: frozenset[str] = frozenset({"draft", "submitted"})

#: Marker state values. ``unconfirmed`` is committed BEFORE the POST and means "a
#: submission may be in flight or may have been accepted - carrier outcome unknown".
#: ``submitted`` is written only after a readable 2xx. Any marker value blocks a retry.
MARKER_STATE_UNCONFIRMED = "unconfirmed"
MARKER_STATE_SUBMITTED = "submitted"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refs(entity: Brand | Campaign) -> dict:
    """A mutable copy of ``carrier_refs`` - always reassigned, never mutated in place
    (``PortableJSON`` does not track in-place mutation)."""
    raw = entity.carrier_refs
    return dict(raw) if isinstance(raw, Mapping) else {}


async def _locked_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    """The campaign row, locked for the rest of this transaction and re-populated from the
    database so a stale caller-supplied instance cannot be read by mistake.

    ``with_for_update`` is a real lock on Postgres and a documented no-op on SQLite (which
    has no ``FOR UPDATE``), so either backend is safe.
    """
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


def _require_fileable(campaign: Campaign) -> None:
    """Only a locally ``draft`` or ``submitted`` campaign may be filed.

    ``submitted`` is admitted solely because the legacy local-only ready-to-file route sets
    it before any Telnyx call; it is NOT a carrier claim and does not by itself permit
    sending (``numbers.can_send`` is approved-only). ``approved``, ``rejected`` and any
    unknown status are refused. The no-carrier-id / no-marker requirement is enforced by
    ``_require_unfiled`` immediately after, so an already-filed campaign still cannot pass.
    """
    status = campaign.status or "draft"
    if status not in _FILEABLE_STATUSES:
        raise ConflictError(
            f"Only a draft or ready-to-file campaign can be filed with Telnyx; this "
            f"campaign is {status!r} - reconcile carrier_refs with Telnyx before retrying"
        )


def _require_unfiled(campaign: Campaign) -> None:
    """Refuse a repeat filing: a stored carrier id or a prior attempt both mean the only
    safe next step is a human reconciling ``carrier_refs`` with the carrier."""
    refs = _refs(campaign)
    if refs.get(PROVIDER):
        raise ConflictError(
            "This campaign already has a Telnyx campaign id; reconcile carrier_refs with "
            "Telnyx before filing it again"
        )
    if refs.get(ATTEMPT_KEY):
        raise ConflictError(
            "A Telnyx filing for this campaign was already attempted and may have been "
            "accepted; reconcile carrier_refs with Telnyx before filing it again"
        )


def _require_attempt_is_ours(campaign: Campaign, *, reference_id: str) -> None:
    """Second, post-POST lock check: the marker we committed must still be the live one,
    and nobody may have recorded a carrier id in the meantime."""
    refs = _refs(campaign)
    if refs.get(PROVIDER):
        raise ConflictError(
            "This campaign already carries a Telnyx campaign id; reconcile carrier_refs "
            "with Telnyx before recording another"
        )
    marker = refs.get(ATTEMPT_KEY)
    if not isinstance(marker, Mapping) or marker.get("reference_id") != reference_id:
        raise ConflictError(
            "The Telnyx filing marker for this campaign changed while the submission was "
            "in flight; reconcile carrier_refs with Telnyx"
        )


async def _approved_brand_with_telnyx_ref(
    session: AsyncSession, campaign: Campaign
) -> tuple[Brand, str]:
    """The campaign's brand, approved locally and carrying a Telnyx brand id.

    Local approval is necessary but NOT sufficient - the carrier verdict is checked
    separately by ``_require_brand_approved_at_telnyx``.
    """
    brand = await session.get(Brand, campaign.brand_id)
    if brand is None:
        raise ValidationFailedError("The campaign's brand no longer exists")
    if brand.status != "approved":
        raise ValidationFailedError(
            "The brand must be approved before its campaigns can be filed with Telnyx"
        )
    raw = _refs(brand).get(PROVIDER)
    telnyx_brand_id = raw.strip() if isinstance(raw, str) else ""
    if not telnyx_brand_id:
        raise ValidationFailedError(
            "The brand is approved locally but has no Telnyx registration; register and "
            "approve the brand with Telnyx before filing its campaigns"
        )
    return brand, telnyx_brand_id


async def _telnyx_carrier_settings(session: AsyncSession, settings: Settings) -> object:
    """The credentials to file with: the org's ACTIVE Telnyx provider account when one
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
        "filing a campaign"
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


async def _require_brand_approved_at_telnyx(
    reg_client: TelnyxRegistrationClient, telnyx_brand_id: str, *, campaign_ref: str
) -> None:
    """Fail-closed carrier-side brand gate.

    A locally approved brand may have been marked approved by an operator rather than by
    Telnyx, and Telnyx can fail a brand after (or between) local approvals - either way a
    campaign filed against it just burns a non-refundable submission. So this runs before
    the attempt marker and before any POST, and every non-approval outcome is fatal:

    * the lookup raising (timeout, transport error, unreadable response, missing brand id)
      propagates untouched - no fallback to local approval, no POST;
    * a brand Telnyx reports as anything other than ``approved`` (via
      ``map_brand_status``, which requires ``status == OK`` AND a verified identity) is
      refused.
    """
    carrier_brand = await reg_client.get_brand(telnyx_brand_id)
    if registration_status.map_brand_status(carrier_brand) != registration_status.APPROVED:
        log.warning(
            "telnyx_campaign_filing_brand_not_approved", campaign_id=campaign_ref
        )
        raise ValidationFailedError(
            "Telnyx does not report the brand as approved; resolve the brand's Telnyx "
            "status before filing a campaign against it"
        )


def _record_attempt(campaign: Campaign, *, reference_id: str) -> None:
    """The pre-POST marker. It is committed BEFORE the carrier call and already states
    that the carrier outcome is unknown, so a timeout or a crash leaves an accurate record
    without any further write: nothing here removes or overwrites this marker on failure."""
    refs = _refs(campaign)
    refs[ATTEMPT_KEY] = {
        "state": MARKER_STATE_UNCONFIRMED,
        "carrier_outcome": "unknown",
        "reference_id": reference_id,
        "attempted_at": _now_iso(),
    }
    campaign.carrier_refs = refs


def _record_filed(
    campaign: Campaign, *, reference_id: str, telnyx_campaign_id: str
) -> None:
    refs = _refs(campaign)
    marker = refs.get(ATTEMPT_KEY)
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    marker.update(
        {
            "state": MARKER_STATE_SUBMITTED,
            "carrier_outcome": "accepted",
            "reference_id": reference_id,
            "campaign_id": telnyx_campaign_id,
            "submitted_at": _now_iso(),
        }
    )
    refs[ATTEMPT_KEY] = marker
    refs[PROVIDER] = telnyx_campaign_id
    campaign.carrier_refs = refs


def _carrier_campaign_id(data: Mapping) -> str:
    """The Telnyx campaign id from a 2xx body, or "" when it is missing/malformed.

    Deliberately not ``str(None)``: only a non-empty string is a usable identifier.
    """
    raw = data.get("campaignId") or data.get("id")
    return raw.strip() if isinstance(raw, str) else ""


async def file_campaign_with_telnyx(
    session: AsyncSession,
    settings: Settings,
    campaign: Campaign,
    *,
    assertions: Mapping[str, bool],
    sub_usecases: Sequence[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Campaign:
    """Submit one local campaign to Telnyx's 10DLC campaignBuilder endpoint.

    ``assertions`` are the caller's explicit consent/financial attestations and are passed
    through untouched (see campaign_payload.build_campaign_payload). ``client`` is an
    optional ``httpx.AsyncClient`` whose transport is injected by tests; when omitted this
    call owns (and closes) its own client.

    Returns the campaign with ``carrier_refs`` updated. Raises ConflictError when a filing
    already exists, the campaign is not a draft/ready-to-file status, ValidationFailedError
    when local state, the brand's carrier status, or the caller's assertions are not
    filable, and FeatureUnavailableError when credentials or the carrier are not usable.
    No exception path advances the campaign's status or removes the committed attempt
    marker.
    """
    if campaign.id is None:
        raise ValidationFailedError("Save the campaign before filing it with Telnyx")
    campaign_id = campaign.id
    reference_id = str(campaign_id)

    # Row lock + authoritative re-read, before any carrier byte: the caller's object (and
    # in particular its carrier_refs) is not trusted. The lock is held until the attempt
    # marker below is committed, so a second concurrent filing blocks here and then sees
    # the marker and is refused rather than double-submitting.
    locked = await _locked_campaign(session, campaign_id)
    _require_fileable(locked)
    _require_unfiled(locked)
    _, telnyx_brand_id = await _approved_brand_with_telnyx_ref(session, locked)

    # Everything local and cheap fails before any credential is decrypted or any byte is
    # sent: build_campaign_payload rejects an incomplete campaign or a bad assertion set.
    payload = build_campaign_payload(
        locked, telnyx_brand_id=telnyx_brand_id, assertions=assertions, sub_usecases=sub_usecases
    )
    # Best-effort de-duplication only; the committed marker is the guard that matters.
    payload[REFERENCE_ID_FIELD] = reference_id

    carrier_settings = await _telnyx_carrier_settings(session, settings)
    api_key = _api_key_from(carrier_settings)

    reg_client = TelnyxRegistrationClient(api_key=api_key, client=client)
    try:
        # Fail-closed carrier check BEFORE the marker and BEFORE the POST: if this raises
        # (unreachable/ambiguous) or the brand is not approved at Telnyx, nothing has been
        # committed and no submission has been spent.
        await _require_brand_approved_at_telnyx(
            reg_client, telnyx_brand_id, campaign_ref=reference_id
        )

        # The attempt marker is made durable BEFORE the POST, because a timed-out
        # campaignBuilder call may still have created a campaign. From this point on the
        # local record says "an attempt exists and its carrier outcome is unknown" until a
        # human reconciles it. Local status is deliberately untouched: a campaign that was
        # already locally `submitted` stays `submitted`, one that was `draft` stays `draft`,
        # and neither can send until it is `approved` (numbers.can_send).
        _record_attempt(locked, reference_id=reference_id)
        await session.commit()  # releases the row lock with the marker durable
        log.info("telnyx_campaign_filing_attempted", campaign_id=reference_id)

        try:
            data = await reg_client.create_campaign(payload)
        except (FeatureUnavailableError, ValidationFailedError):
            # The marker stays committed with carrier_outcome=unknown and the campaign
            # keeps whatever local status it had: the carrier may have accepted the
            # submission even though we cannot tell.
            log.warning("telnyx_campaign_filing_unconfirmed", campaign_id=reference_id)
            raise

        telnyx_campaign_id = _carrier_campaign_id(data)
        if not telnyx_campaign_id:
            # A 2xx we cannot read is still a submission we may not repeat: keep the
            # marker (carrier outcome stays unknown) and make a human reconcile it.
            log.warning(
                "telnyx_campaign_filing_unconfirmed_response", campaign_id=reference_id
            )
            raise FeatureUnavailableError(
                "Telnyx accepted the campaign submission but returned no usable campaign "
                "id; reconcile carrier_refs with Telnyx before retrying"
            )

        # Re-lock and re-read: the earlier commit released the row, and the marker written
        # under it must still be the live one before we record the carrier id.
        locked = await _locked_campaign(session, campaign_id)
        _require_attempt_is_ours(locked, reference_id=reference_id)
        _record_filed(
            locked, reference_id=reference_id, telnyx_campaign_id=telnyx_campaign_id
        )
        registration.advance_status(locked, "submitted")
        await session.commit()
        log.info("telnyx_campaign_filing_submitted", campaign_id=reference_id)
        return locked
    finally:
        await reg_client.aclose()
