from __future__ import annotations

import logging
import time

import httpx
import jwt

#: Poisoned-batch protection: one oversized or bad batch must not force re-buffering
#: (and re-sending) everything the worker has accumulated. Matches the backend's own
#: MAX_TRANSCRIPT_BATCH (agent_svc) - a chunk this size or smaller always clears that
#: server-side cap regardless of how large `segments` grows.
TRANSCRIPT_CHUNK_SIZE = 200


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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
