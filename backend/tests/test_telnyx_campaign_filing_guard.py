"""Guard tests for ``file_campaign_with_telnyx``: each refusal must fire before any
credential is resolved or any carrier byte is sent."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.errors import ConflictError
from app.models.numbers import Campaign
from app.services import telnyx_campaign_filing as filing

pytestmark = pytest.mark.asyncio


class _Unreachable:
    """Any attribute access is a bug: these paths fail before DB or credentials."""

    def __getattr__(self, name: str):
        raise AssertionError(f"{name} must not be reached; the guard refuses first")


def _campaign(status: str, refs: dict | None = None) -> Campaign:
    return Campaign(
        id=uuid.uuid4(), brand_id=uuid.uuid4(), status=status, carrier_refs=refs or {}
    )


async def _attempt_filing(monkeypatch, campaign: Campaign, seen: list) -> None:
    async def _locked_campaign(session, campaign_id):
        return campaign

    monkeypatch.setattr(filing, "_locked_campaign", _locked_campaign)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("no carrier request may be issued")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await filing.file_campaign_with_telnyx(
            _Unreachable(), _Unreachable(), campaign, assertions={}, client=client
        )


async def test_terminal_status_is_refused_before_any_carrier_work(monkeypatch):
    campaign = _campaign("approved")
    seen: list[httpx.Request] = []
    with pytest.raises(ConflictError, match="draft or ready-to-file"):
        await _attempt_filing(monkeypatch, campaign, seen)
    assert seen == []


async def test_prior_filing_marker_is_refused_before_any_carrier_work(monkeypatch):
    campaign = _campaign(
        "submitted",
        {
            filing.ATTEMPT_KEY: {
                "state": filing.MARKER_STATE_UNCONFIRMED,
                "carrier_outcome": "unknown",
                "reference_id": "prior-attempt",
            }
        },
    )
    seen: list[httpx.Request] = []
    with pytest.raises(ConflictError, match="already attempted"):
        await _attempt_filing(monkeypatch, campaign, seen)
    assert seen == []
    assert filing.PROVIDER not in campaign.carrier_refs
    assert campaign.status == "submitted"
