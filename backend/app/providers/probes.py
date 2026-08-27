"""Credential probes: does this key actually work?

phase-9b DR-4. A probe is ONE cheap authenticated read against the carrier, run only when
an operator asks. Three properties are deliberate:

* **Never on boot.** A probe on every restart turns a credential typo into a rate-limit
  ban, and a startup that silently "checks" credentials teaches people to read the absence
  of an error as proof of health. It is not.
* **Read-only.** Every probe below is a GET against an account/profile resource. A probe
  must never create, send or spend anything.
* **The carrier's own words.** On failure we surface the carrier's message verbatim
  (truncated). "Authentication failed" from Twilio is worth ten of our paraphrases when
  somebody is staring at a key they just pasted.

This module lives OUTSIDE the adapters on purpose: probing is an operator concern, not a
send-path concern, and the carrier abstraction (providers/domain.py) stays frozen.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger("carrier.probe")

TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    #: Operator-facing. On failure this is the CARRIER's message where we have one.
    detail: str
    #: What we actually called, so the result is auditable rather than magic.
    checked: str = ""


async def probe(name: str, settings, *, client: httpx.AsyncClient | None = None) -> ProbeResult:  # noqa: ANN001
    """Verify one carrier's credentials. Never raises - a probe that blows up is itself a
    failed probe, and the operator needs the reason, not a stack trace."""
    fn = _PROBES.get(name)
    if fn is None:
        return ProbeResult(name, False, f"No credential probe is implemented for {name!r}.")
    if not settings.carrier_live(name):
        missing = [
            k
            for k, v in settings.carrier_requirements().get(name, {}).items()
            if not str(v.get_secret_value() if hasattr(v, "get_secret_value") else v or "")
        ]
        return ProbeResult(
            name,
            False,
            "Not configured; add " + ", ".join(missing) + " first."
            if missing
            else f"{name.upper()}_ENABLED is false.",
        )

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        return await fn(settings, http)
    except httpx.TransportError as exc:
        return ProbeResult(name, False, f"Could not reach {name}: {exc}"[:255])
    except Exception as exc:  # noqa: BLE001 - a probe never propagates
        log.warning("probe_unexpected_error", carrier=name, error=str(exc))
        return ProbeResult(name, False, f"Probe failed unexpectedly: {exc}"[:255])
    finally:
        if owns_client:
            await http.aclose()


def _secret(v) -> str:  # noqa: ANN001
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v or "")


def _detail_from(resp: httpx.Response) -> str:
    """The carrier's own error text, whatever shape it arrived in."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or f"HTTP {resp.status_code}")[:255]
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "error_message"):
            if body.get(key):
                return str(body[key])[:255]
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("detail"):
                return str(first["detail"])[:255]
    return f"HTTP {resp.status_code}"


async def _probe_telnyx(settings, http: httpx.AsyncClient) -> ProbeResult:  # noqa: ANN001
    url = "https://api.telnyx.com/v2/messaging_profiles?page[size]=1"
    resp = await http.get(
        url, headers={"Authorization": f"Bearer {_secret(settings.telnyx_api_key)}"}
    )
    if resp.status_code == 200:
        return ProbeResult("telnyx", True, "Credentials accepted.", url)
    return ProbeResult("telnyx", False, _detail_from(resp), url)


async def _probe_twilio(settings, http: httpx.AsyncClient) -> ProbeResult:  # noqa: ANN001
    sid = settings.twilio_account_sid
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
    resp = await http.get(url, auth=(sid, _secret(settings.twilio_auth_token)))
    if resp.status_code == 200:
        return ProbeResult("twilio", True, "Credentials accepted.", url)
    return ProbeResult("twilio", False, _detail_from(resp), url)


async def _probe_plivo(settings, http: httpx.AsyncClient) -> ProbeResult:  # noqa: ANN001
    auth_id = settings.plivo_auth_id
    url = f"https://api.plivo.com/v1/Account/{auth_id}/"
    resp = await http.get(url, auth=(auth_id, _secret(settings.plivo_auth_token)))
    if resp.status_code == 200:
        return ProbeResult("plivo", True, "Credentials accepted.", url)
    return ProbeResult("plivo", False, _detail_from(resp), url)


async def _probe_signalwire(settings, http: httpx.AsyncClient) -> ProbeResult:  # noqa: ANN001
    space = settings.signalwire_space_url.rstrip("/")
    project = settings.signalwire_project_id
    url = f"https://{space}/api/laml/2010-04-01/Accounts/{project}.json"
    resp = await http.get(url, auth=(project, _secret(settings.signalwire_api_token)))
    if resp.status_code == 200:
        return ProbeResult("signalwire", True, "Credentials accepted.", url)
    return ProbeResult("signalwire", False, _detail_from(resp), url)


async def _probe_bandwidth(settings, http: httpx.AsyncClient) -> ProbeResult:  # noqa: ANN001
    """Probe the surface this deployment actually uses.

    Bandwidth sells voice and messaging separately. Testing the messaging endpoint on a
    voice-only account asks a question the account was never meant to answer, and reports
    a 401 that says nothing about whether calls would work.
    """
    account = settings.bandwidth_account_id
    if not settings.bandwidth_messaging_application_id.strip():
        url = f"https://voice.bandwidth.com/api/v2/accounts/{account}/calls"
    else:
        url = f"https://messaging.bandwidth.com/api/v2/users/{account}/media"
    if settings.bandwidth_auth_mode == "oauth2":
        # Probe the way the adapter authenticates, or the probe answers a question
        # nobody asked: Basic fails on an OAuth2 account even when the account is fine.
        from app.providers.bandwidth.auth import BandwidthAuthError, BandwidthTokenProvider

        provider = BandwidthTokenProvider(
            settings.bandwidth_api_username,
            _secret(settings.bandwidth_api_password),
            client=http,
        )
        try:
            token = await provider.token()
        except BandwidthAuthError as exc:
            return ProbeResult("bandwidth", False, str(exc)[:255], provider._token_url)
        resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
    else:
        resp = await http.get(
            url,
            auth=(settings.bandwidth_api_username, _secret(settings.bandwidth_api_password)),
        )
    if resp.status_code == 200:
        return ProbeResult("bandwidth", True, "Credentials accepted.", url)
    if resp.status_code in (401, 403):
        return ProbeResult(
            "bandwidth",
            False,
            "Bandwidth rejected these credentials. Both the Voice and Messaging APIs "
            "expect an API-user credential pair created under Account -> Credentials; "
            "that is NOT the dashboard login, and a Client ID / Client Secret pair "
            "(CLI-...) belongs to a different product surface and will 401 here. "
            + _detail_from(resp),
            url,
        )
    return ProbeResult("bandwidth", False, _detail_from(resp), url)


_PROBES = {
    "telnyx": _probe_telnyx,
    "twilio": _probe_twilio,
    "plivo": _probe_plivo,
    "signalwire": _probe_signalwire,
    "bandwidth": _probe_bandwidth,
}
