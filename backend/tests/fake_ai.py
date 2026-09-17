"""A stand-in for DeepSeek Flash in tests (P43). Tests never call the real AI.

``FakeSafetyAI`` answers ``services/ai_guard.judge`` requests through an httpx.MockTransport.
It recognises the task from the system prompt and answers with a configurable reply:

    ai = FakeSafetyAI()
    ai.document = {...}            # what the document reader "reads"
    ai.decision = {...}            # the KYC decision pack
    ai.text_verdict = lambda body: {...}   # outbound text checks
    ai.call_verdict = lambda transcript: {...}
    with ai.installed(): ...
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone

import httpx

from app.services import ai_guard


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class FakeSafetyAI:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail = False
        self.document: dict = {
            "readable": True,
            "document_type": "utility_bill",
            "issuer": "Austin Energy",
            "document_date": _today(),
            "names": ["Jane Smith"],
            "address": "12 Oak St, Austin, TX 78702",
            "company_name": None,
            "registration_number": None,
            "tax_number": None,
            "jurisdiction": None,
            "company_status": None,
            "name_matches": True,
            "address_matches": True,
            "company_matches": True,
            "number_matches": True,
            "looks_edited": False,
            "notes": "Recent utility bill; name and address match.",
        }
        self.decision: dict = {
            "recommendation": "approve",
            "confidence": 88,
            "summary": "Established plumbing business; owner verified; documents match.",
            "thoughts": [{"area": "identity", "assessment": "ok", "thought": "Owner verified."}],
            "concerns": [],
            "questions_for_applicant": [],
            "suggested_risk": "standard",
            "suggested_limits": {"daily_calls": 500, "daily_texts": 1000, "max_numbers": 5},
            "note_for_decision": "Identity and company confirmed.",
        }
        self.text_verdict = lambda body: {
            "verdict": "allow",
            "category": "none",
            "confidence": 90,
            "reason": "Ordinary business message.",
        }
        self.call_verdict = lambda transcript: {
            "verdict": "ok",
            "confidence": 90,
            "summary": "Ordinary business call.",
            "evidence": [],
            "category": "none",
        }
        self.generic: dict = {}

    def _answer(self, payload: dict) -> dict:
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        text = user if isinstance(user, str) else " ".join(
            part.get("text", "") for part in user if isinstance(part, dict)
        )
        if "verification documents" in system:
            return self.document
        if "senior compliance analyst" in system:
            return self.decision
        if "outbound text message" in system:
            return self.text_verdict(text)
        if "call transcript" in system:
            return self.call_verdict(text)
        return self.generic

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.fail:
            return httpx.Response(503, json={"error": {"message": "down"}})
        answer = self._answer(payload)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    def tasks(self, marker: str) -> list[dict]:
        return [r for r in self.requests if marker in r["messages"][0]["content"]]

    @contextlib.contextmanager
    def installed(self):
        ai_guard.set_client_factory(
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        )
        try:
            yield self
        finally:
            ai_guard.set_client_factory(None)
