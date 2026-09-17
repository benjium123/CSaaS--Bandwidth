"""P41 risky-login detection.

A flagged login is NOT refused (operator decision 2026-09-16: real owners travel). It must
still pass a fresh second factor, the account holder and the owners of their workspaces are
told, and the operator review queue gets an entry. No selfie for logins.

Signals, all best-effort and all degrading to "no signal" rather than to "flag everything":
  - country_not_allowed  GeoLite2 country outside KYC_COUNTRIES
  - tor                  IP is on the Tor exit list (sweeper keeps it fresh)
  - datacenter           GeoLite2 ASN belongs to a hosting / VPN / cloud network
  - new_device           X-Device-Id never seen for this user (hash only is stored)
  - country_changed      country differs from the user's last device sighting

Missing GeoLite2 files => country/datacenter are skipped (reported by provider_statuses);
a missing Tor list => tor is skipped. Private and loopback addresses never flag on network
signals - that would flag every developer and every request behind a local proxy.
"""

from __future__ import annotations

import hashlib
import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import LoginDevice, SecurityAlert, User
from app.services import identity as identity_svc

log = structlog.get_logger(__name__)

FLAG_LABELS: dict[str, str] = {
    "country_not_allowed": "signed in from outside the countries we serve",
    "tor": "signed in through the Tor network",
    "datacenter": "signed in through a VPN, proxy or hosting network",
    "new_device": "signed in from a new device",
    "country_changed": "signed in from a different country than last time",
    "recovery_code": "signed in with a one-time recovery code",
    "identity_recovery": "recovered the account with an ID and selfie check",
}

#: Substrings of ASN organisation names that mean hosting, cloud or commercial VPN rather
#: than a residential / business ISP. Deliberately conservative: a false "datacenter" only
#: costs a second-factor prompt the user was going to see anyway plus an alert.
DATACENTER_ASN_MARKERS: tuple[str, ...] = (
    "amazon",
    "aws",
    "google cloud",
    "microsoft",
    "azure",
    "digitalocean",
    "ovh",
    "hetzner",
    "linode",
    "akamai",
    "vultr",
    "choopa",
    "oracle",
    "alibaba",
    "tencent",
    "contabo",
    "leaseweb",
    "m247",
    "datacamp",
    "cdn77",
    "hostinger",
    "scaleway",
    "hosting",
    "server",
    "data center",
    "datacenter",
    "vpn",
    "proxy",
    "colocation",
)

DEVICE_HEADER = "x-device-id"


@dataclass
class LoginRisk:
    flags: list[str] = field(default_factory=list)
    ip: str | None = None
    country: str | None = None
    asn_org: str | None = None
    device_hash: str | None = None

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    def describe(self) -> list[str]:
        return [FLAG_LABELS.get(f, f) for f in self.flags]


# --------------------------------------------------------------------------------------
# Lookups - module-level functions so tests monkeypatch them instead of shipping .mmdb files
# --------------------------------------------------------------------------------------
_readers: dict[str, object] = {}
_tor_cache: dict[str, object] = {"mtime": None, "ips": frozenset()}


def _is_public(ip: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(ip)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def _reader(settings: Settings, name: str):
    directory = settings.geolite2_dir.strip()
    if not directory:
        return None
    path = Path(directory) / name
    key = str(path)
    if key in _readers:
        return _readers[key]
    if not path.exists():
        return None
    try:
        import maxminddb

        reader = maxminddb.open_database(key)
    except Exception:
        log.warning("geolite2_open_failed", path=key, exc_info=True)
        return None
    _readers[key] = reader
    return reader


def lookup_country(settings: Settings, ip: str | None) -> str | None:
    if not _is_public(ip):
        return None
    reader = _reader(settings, "GeoLite2-Country.mmdb")
    if reader is None:
        return None
    try:
        record = reader.get(ip) or {}  # type: ignore[attr-defined]
    except Exception:
        return None
    country = (record.get("country") or record.get("registered_country") or {}).get("iso_code")
    return country.upper() if isinstance(country, str) else None


def lookup_asn_org(settings: Settings, ip: str | None) -> str | None:
    if not _is_public(ip):
        return None
    reader = _reader(settings, "GeoLite2-ASN.mmdb")
    if reader is None:
        return None
    try:
        record = reader.get(ip) or {}  # type: ignore[attr-defined]
    except Exception:
        return None
    org = record.get("autonomous_system_organization")
    return org if isinstance(org, str) else None


def tor_exit_path(settings: Settings) -> Path:
    return Path(settings.security_data_dir) / "tor_exits.txt"


def is_tor_exit(settings: Settings, ip: str | None) -> bool:
    if not _is_public(ip):
        return False
    path = tor_exit_path(settings)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if _tor_cache["mtime"] != mtime:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        _tor_cache["ips"] = frozenset(
            line.strip() for line in lines if line.strip() and not line.startswith("#")
        )
        _tor_cache["mtime"] = mtime
    return ip in _tor_cache["ips"]  # type: ignore[operator]


def is_datacenter_asn(asn_org: str | None) -> bool:
    if not asn_org:
        return False
    lowered = asn_org.lower()
    return any(marker in lowered for marker in DATACENTER_ASN_MARKERS)


def hash_device_id(raw: str | None) -> str | None:
    value = (raw or "").strip()
    # A real client id is a random 16+ byte token. Anything shorter is treated as absent
    # rather than as a device, so a blank or junk header cannot be "remembered".
    if len(value) < 16 or len(value) > 256:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


async def refresh_tor_exit_list(settings: Settings, client=None) -> int:
    """Download the Tor exit list atomically. Returns the number of addresses stored."""
    import httpx

    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.get(settings.tor_exit_list_url)
        resp.raise_for_status()
        ips = [
            line.strip()
            for line in resp.text.splitlines()
            if line.strip() and not line.startswith("#") and _is_public(line.strip())
        ]
    finally:
        if owns:
            await client.aclose()
    if not ips:
        # An empty or garbage response must never wipe a good list.
        return 0
    path = tor_exit_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(ips) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(ips)


# --------------------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------------------
async def assess(
    session: AsyncSession, settings: Settings, request: Request, user: User
) -> LoginRisk:
    ip = identity_svc.client_ip(request)
    risk = LoginRisk(ip=ip)
    risk.country = lookup_country(settings, ip)
    risk.asn_org = lookup_asn_org(settings, ip)
    risk.device_hash = hash_device_id(request.headers.get(DEVICE_HEADER))

    if risk.country and risk.country not in settings.kyc_country_list:
        risk.flags.append("country_not_allowed")
    if is_tor_exit(settings, ip):
        risk.flags.append("tor")
    if is_datacenter_asn(risk.asn_org):
        risk.flags.append("datacenter")

    last_country = (
        await session.execute(
            sa.select(LoginDevice.last_country)
            .where(LoginDevice.user_id == user.id, LoginDevice.last_country.is_not(None))
            .order_by(LoginDevice.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    has_any_device = (
        await session.execute(
            sa.select(sa.func.count(LoginDevice.id)).where(LoginDevice.user_id == user.id)
        )
    ).scalar_one()

    if risk.device_hash is not None:
        known = (
            await session.execute(
                sa.select(LoginDevice.id).where(
                    LoginDevice.user_id == user.id,
                    LoginDevice.device_hash == risk.device_hash,
                )
            )
        ).scalar_one_or_none()
        # The very first device an account ever uses is not "new" - there is nothing to
        # compare it with, and flagging every signup would drown the queue.
        if known is None and has_any_device:
            risk.flags.append("new_device")
    elif has_any_device:
        # No device id at all from an account that has used one before: treat as new.
        risk.flags.append("new_device")

    if risk.country and last_country and risk.country != last_country:
        risk.flags.append("country_changed")
    return risk


async def remember_device(session: AsyncSession, user: User, risk: LoginRisk) -> None:
    """Called only AFTER the second factor succeeds - a device is never remembered on the
    strength of a password alone."""
    if risk.device_hash is None:
        return
    now = datetime.now(timezone.utc)
    row = (
        await session.execute(
            sa.select(LoginDevice).where(
                LoginDevice.user_id == user.id, LoginDevice.device_hash == risk.device_hash
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            LoginDevice(
                id=uuid.uuid4(),
                user_id=user.id,
                device_hash=risk.device_hash,
                first_seen_at=now,
                last_seen_at=now,
                last_ip=risk.ip,
                last_country=risk.country,
            )
        )
        return
    row.last_seen_at = now
    row.last_ip = risk.ip
    if risk.country:
        row.last_country = risk.country


def open_alert(
    session: AsyncSession, user: User, risk: LoginRisk, request: Request
) -> SecurityAlert:
    row = SecurityAlert(
        id=uuid.uuid4(),
        kind="flagged_login",
        user_id=user.id,
        status="open",
        detail={
            "email": user.email,
            "flags": list(risk.flags),
            "reasons": risk.describe(),
            "ip": risk.ip,
            "country": risk.country,
            "network": risk.asn_org,
            "user_agent": identity_svc.client_user_agent(request),
        },
    )
    session.add(row)
    return row
