import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import server


def test_auth_token_deterministic_and_differs():
    assert server.auth_token("secret") == server.auth_token("secret")
    assert server.auth_token("secret") != server.auth_token("other")
    assert len(server.auth_token("secret")) == 64


def test_authorized_bearer():
    token = server.auth_token("secret")
    assert server.authorized(f"Bearer {token}", token) is True
    assert server.authorized(token, token) is False
    assert server.authorized(f"Bearer wrong", token) is False
    assert server.authorized(None, token) is False


def test_is_hallucination():
    assert server.is_hallucination("ARE ARE ARE ARE") is True
    assert server.is_hallucination("yes yes") is False
    assert server.is_hallucination("I am here") is False


def test_clean_text_with_punct():
    class FakePunct:
        def add_punctuation_with_case(self, text):
            return text.capitalize() + "."

    assert server.clean_text("  hello world  ", FakePunct()) == "Hello world."
    assert server.clean_text("  ", None) == ""
    assert server.clean_text("ARE ARE ARE ARE", None) == ""


def test_fixed_chunks():
    samples = np.zeros(45 * 16000, dtype=np.float32)
    chunks = server.fixed_chunks(samples, 20)
    assert len(chunks) == 3
    assert [c[0] for c in chunks] == [0, 320000, 640000]
    assert len(chunks[0][1]) == 320000
    assert len(chunks[1][1]) == 320000
    assert len(chunks[2][1]) == 80000


def test_rms_zeros():
    samples = np.zeros(16000, dtype=np.float32)
    assert server.rms(samples) == 0.0


def test_to_segments_ms_math_and_skips_empty():
    pieces = [(16000, 16000, "hi"), (32000, 16000, "")]
    segments = server.to_segments(0, pieces)
    assert len(segments) == 1
    assert segments[0]["channel"] == 0
    assert segments[0]["start_ms"] == 1000
    assert segments[0]["end_ms"] == 2000
    assert segments[0]["text"] == "hi"


def test_server_handler_no_models():
    token = server.auth_token("test-secret")
    srv = server.make_server(0, token, {}, None, None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    port = srv.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 401

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert data["engines"] == []

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/transcribe?engine=parakeet",
            data=b"audio",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Length": "5",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
