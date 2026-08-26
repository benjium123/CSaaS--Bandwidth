from __future__ import annotations

import logging
import time
from collections.abc import Iterable

import httpx
import jwt

#: Poisoned-batch protection: one oversized or bad batch must not force re-buffering
#: (and re-sending) everything the worker has accumulated. Matches the backend's own
#: MAX_TRANSCRIPT_BATCH (agent_svc) - a chunk this size or smaller always clears that
#: server-side cap regardless of how large `segments` grows.
TRANSCRIPT_CHUNK_SIZE = 200


def format_handoff_summary(transcript_tail: Iterable[tuple[str, str]]) -> str:
    """Render (role, text) transcript entries as a "role: text" per-line summary for
    the POST /api/v1/agent/handoff payload. Pure and livekit-free (unlike ai_agent.py,
    which imports the SDK at module scope) so it is unit-testable under the plain
    backend venv - `transcript_tail` only needs to be an iterable of (role, text)
    pairs, e.g. the live call's `collections.deque(maxlen=6)`.
    """
    return "\n".join(f"{role}: {text}" for role, text in transcript_tail)


class BackendClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._logger = logging.getLogger(__name__)

    def _token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._api_key,
            "sub": "agent-worker",
            "exp": now + 300,
        }
        encoded = jwt.encode(claims, self._api_secret, algorithm="HS256")
        return encoded if isinstance(encoded, str) else encoded.decode("utf-8")

    async def fetch_context(self, call_id: str) -> dict | None:
        url = f"{self._base_url}/api/v1/agent/context/{call_id}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            response = await self._client.get(url, headers=headers)
            if response.status_code == 200:
                return response.json()
            self._logger.warning(
                "fetch_context call_id=%s status=%s", call_id, response.status_code
            )
        except Exception:
            self._logger.exception("fetch_context call_id=%s failed", call_id)
        return None

    async def post_transcript(self, call_id: str, segments: list) -> list:
        """POST `segments` in chunks of ``TRANSCRIPT_CHUNK_SIZE``, all-or-nothing per
        chunk, so one poisoned/oversized batch cannot force the caller to re-buffer
        (and keep re-sending) segments that already landed. Returns the sublist of
        `segments` that was NOT accepted: once a chunk fails, that chunk AND every
        chunk after it are reported as not-accepted (in original order) without
        attempting to send them - the caller re-buffers exactly that contiguous tail.
        """
        not_accepted: list = []
        for start in range(0, len(segments), TRANSCRIPT_CHUNK_SIZE):
            chunk = segments[start : start + TRANSCRIPT_CHUNK_SIZE]
            if not_accepted:
                not_accepted.extend(chunk)
                continue
            if not await self._post_chunk(call_id, chunk):
                not_accepted.extend(chunk)
        return not_accepted

    async def _post_chunk(self, call_id: str, segments: list) -> bool:
        url = f"{self._base_url}/api/v1/agent/transcript"
        headers = {"Authorization": f"Bearer {self._token()}"}
        payload = {"call_id": call_id, "segments": segments}
        try:
            response = await self._client.post(url, headers=headers, json=payload)
            if 200 <= response.status_code < 300:
                return True
            self._logger.warning(
                "post_transcript call_id=%s status=%s segments=%d",
                call_id,
                response.status_code,
                len(segments),
            )
        except Exception:
            self._logger.exception("post_transcript call_id=%s failed", call_id)
        return False

    async def get_contact(self, call_id: str, e164: str) -> dict | None:
        url = f"{self._base_url}/api/v1/agent/contact/{e164}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            response = await self._client.get(
                url, headers=headers, params={"call_id": call_id}
            )
            if response.status_code == 200:
                return response.json()
            self._logger.warning(
                "get_contact call_id=%s e164=%s status=%s",
                call_id,
                e164,
                response.status_code,
            )
        except Exception:
            self._logger.exception(
                "get_contact call_id=%s e164=%s failed", call_id, e164
            )
        return None

    async def book_appointment(
        self, call_id: str, contact_e164: str, raw_when: str, notes: str = ""
    ) -> dict | None:
        url = f"{self._base_url}/api/v1/agent/appointments"
        headers = {"Authorization": f"Bearer {self._token()}"}
        payload = {
            "call_id": call_id,
            "contact_e164": contact_e164,
            "raw_when": raw_when,
            "notes": notes,
        }
        try:
            response = await self._client.post(url, headers=headers, json=payload)
            if response.status_code == 201:
                return response.json()
            self._logger.warning(
                "book_appointment call_id=%s status=%s", call_id, response.status_code
            )
        except Exception:
            self._logger.exception("book_appointment call_id=%s failed", call_id)
        return None

    async def kb_search(self, call_id: str, query: str) -> list:
        url = f"{self._base_url}/api/v1/agent/kb/search"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            response = await self._client.get(
                url, headers=headers, params={"call_id": call_id, "q": query}
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("chunks") or []
            self._logger.warning(
                "kb_search call_id=%s status=%s", call_id, response.status_code
            )
        except Exception:
            self._logger.exception("kb_search call_id=%s failed", call_id)
        return []

    async def post_handoff(self, call_id: str, reason: str, summary: str) -> bool:
        url = f"{self._base_url}/api/v1/agent/handoff"
        headers = {"Authorization": f"Bearer {self._token()}"}
        payload = {"call_id": call_id, "reason": reason, "summary": summary}
        try:
            response = await self._client.post(url, headers=headers, json=payload)
            if 200 <= response.status_code < 300:
                return True
            self._logger.warning(
                "post_handoff call_id=%s status=%s", call_id, response.status_code
            )
        except Exception:
            self._logger.exception("post_handoff call_id=%s failed", call_id)
        return False

    async def post_amd(self, call_id: str, result: str) -> bool:
        url = f"{self._base_url}/api/v1/agent/amd"
        headers = {"Authorization": f"Bearer {self._token()}"}
        payload = {"call_id": call_id, "result": result}
        try:
            response = await self._client.post(url, headers=headers, json=payload)
            if 200 <= response.status_code < 300:
                return True
            self._logger.warning(
                "post_amd call_id=%s status=%s result=%s",
                call_id,
                response.status_code,
                result,
            )
        except Exception:
            self._logger.exception("post_amd call_id=%s failed", call_id)
        return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
