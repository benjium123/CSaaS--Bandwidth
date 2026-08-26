"""F4: poisoned-batch protection. post_transcript must chunk into batches of
TRANSCRIPT_CHUNK_SIZE, all-or-nothing per chunk, and report back only the
FAILED-and-later segments as not-accepted - earlier, already-accepted chunks must
never be resent."""

from __future__ import annotations

import pytest

from agents.backend_client import TRANSCRIPT_CHUNK_SIZE, BackendClient


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self):  # pragma: no cover - not exercised here
        return {}


class _FakeAsyncClient:
    """Records every POST body and fails from `fail_from_call` (1-indexed) onward."""

    def __init__(self, fail_from_call: int | None = None) -> None:
        self.fail_from_call = fail_from_call
        self.post_calls: list[list] = []

    async def post(self, url, headers=None, json=None):  # noqa: ANN001
        self.post_calls.append(json["segments"])
        call_number = len(self.post_calls)
        if self.fail_from_call is not None and call_number >= self.fail_from_call:
            return _FakeResponse(500)
        return _FakeResponse(200)

    async def get(self, url, headers=None):  # pragma: no cover - unused here
        return _FakeResponse(200)

    async def aclose(self) -> None:
        pass


def _segments(n: int) -> list[dict]:
    return [{"role": "user", "text": f"seg-{i}", "at_ms": i} for i in range(n)]


@pytest.mark.asyncio
async def test_batch_over_chunk_size_is_split_into_multiple_posts() -> None:
    fake = _FakeAsyncClient()
    client = BackendClient("http://backend", "key", "secret", client=fake)

    total = TRANSCRIPT_CHUNK_SIZE * 2 + 37
    not_accepted = await client.post_transcript("call-1", _segments(total))

    assert not_accepted == []
    assert len(fake.post_calls) == 3
    assert [len(c) for c in fake.post_calls] == [
        TRANSCRIPT_CHUNK_SIZE,
        TRANSCRIPT_CHUNK_SIZE,
        37,
    ]


@pytest.mark.asyncio
async def test_a_failing_chunk_does_not_poison_or_resend_earlier_chunks() -> None:
    # 3 chunks total; the 2nd fails. The 1st must be posted exactly once (never
    # retried here - that is the caller's job) and the 3rd must never even be sent.
    fake = _FakeAsyncClient(fail_from_call=2)
    client = BackendClient("http://backend", "key", "secret", client=fake)

    total = TRANSCRIPT_CHUNK_SIZE * 2 + 37
    segments = _segments(total)
    not_accepted = await client.post_transcript("call-1", segments)

    # Only 2 HTTP calls made: the 3rd chunk is short-circuited once a chunk fails.
    assert len(fake.post_calls) == 2
    assert fake.post_calls[0] == segments[:TRANSCRIPT_CHUNK_SIZE]
    assert fake.post_calls[1] == segments[TRANSCRIPT_CHUNK_SIZE : TRANSCRIPT_CHUNK_SIZE * 2]

    # not_accepted is exactly the failing chunk + the untried tail, in order.
    assert not_accepted == segments[TRANSCRIPT_CHUNK_SIZE:]


@pytest.mark.asyncio
async def test_all_chunks_succeed_returns_empty_not_accepted() -> None:
    fake = _FakeAsyncClient()
    client = BackendClient("http://backend", "key", "secret", client=fake)

    not_accepted = await client.post_transcript("call-1", _segments(5))
    assert not_accepted == []
    assert len(fake.post_calls) == 1
