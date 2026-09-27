import hashlib
import hmac
import json
import os
import resource
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

SAMPLE_RATE = 16000
MAX_BODY = 150 * 1024 * 1024
SILENCE_RMS = 0.003
CHUNK_SECONDS = 20
CHUNK_SAMPLES = CHUNK_SECONDS * SAMPLE_RATE


class AudioDecodeError(Exception):
    pass


def auth_token(secret: str) -> str:
    return hashlib.sha256(("stt:" + secret).encode()).hexdigest()


def authorized(header: str | None, token: str) -> bool:
    if not header:
        return False
    if not header.startswith("Bearer "):
        return False
    provided = header[len("Bearer "):].strip()
    return hmac.compare_digest(provided, token)


def is_hallucination(text: str) -> bool:
    words = []
    for word in text.split():
        cleaned = word.strip(string.punctuation)
        if cleaned:
            words.append(cleaned.lower())
    return len(words) >= 3 and len(set(words)) == 1


#: A segment that is only one of these is noise the models emit on breaths and line hiss
#: (seen on the first live test call); real one-word answers ("Yeah", "No") are kept.
NOISE_WORDS = frozenset({"the", "a", "an", "um", "uh", "ah", "er", "hmm", "mm", "and", "i"})


def clean_text(text: str, punct=None) -> str:
    if not text or not text.strip():
        return ""
    text = text.strip()
    if punct is not None:
        text = punct.add_punctuation_with_case(text.lower()).strip()
    if not text or is_hallucination(text):
        return ""
    if text.strip(" .,!?").lower() in NOISE_WORDS:
        return ""
    return text


def fixed_chunks(samples, seconds):
    chunk = int(seconds * SAMPLE_RATE)
    if chunk <= 0 or len(samples) == 0:
        return []
    result = []
    start = 0
    n = len(samples)
    while start < n:
        end = min(start + chunk, n)
        result.append((start, samples[start:end]))
        start = end
    return result


def rms(samples):
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def to_segments(channel, pieces):
    result = []
    for start_sample, n_samples, text in pieces:
        text = text.strip()
        if not text:
            continue
        start_ms = int(start_sample * 1000 / SAMPLE_RATE)
        end_ms = int((start_sample + n_samples) * 1000 / SAMPLE_RATE)
        result.append({"channel": channel, "start_ms": start_ms, "end_ms": end_ms, "text": text})
    return result


def probe_channels(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=channels",
        "-of", "csv=p=0",
        path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if proc.returncode != 0:
            raise AudioDecodeError()
        out = proc.stdout.strip()
        if not out.isdigit():
            return 1
        ch = int(out)
        return max(1, min(2, ch))
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise AudioDecodeError() from exc


def decode_channel(path, channel):
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path,
        "-af", f"pan=mono|c0=c{channel},dynaudnorm=f=150:g=15",
        "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if proc.returncode != 0:
            raise AudioDecodeError()
        samples = np.frombuffer(proc.stdout, dtype=np.float32)
        if samples.size == 0:
            raise AudioDecodeError()
        return samples
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise AudioDecodeError() from exc


def speech_segments(samples, vad_path):
    if vad_path is None or not os.path.exists(vad_path):
        return fixed_chunks(samples, CHUNK_SECONDS)

    import sherpa_onnx

    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = vad_path
    cfg.silero_vad.threshold = 0.3
    cfg.silero_vad.min_silence_duration = 0.5
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 20.0
    cfg.sample_rate = SAMPLE_RATE

    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
    segments = []
    step = 512
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        vad.accept_waveform(chunk)
        while not vad.empty():
            front = vad.front
            segments.append((front.start, front.samples))
            vad.pop()

    vad.flush()
    while not vad.empty():
        front = vad.front
        segments.append((front.start, front.samples))
        vad.pop()

    if segments:
        return sorted(segments + missed_speech(samples, segments), key=lambda seg: seg[0])
    return fixed_chunks(samples, CHUNK_SECONDS)


def missed_speech(samples, segments, frame=1600, min_frames=4, pad=3200, max_len=20 * SAMPLE_RATE):
    """Loud stretches the VAD skipped. Phone audio at 8 kHz fools Silero on short, quiet
    lines ("Hello?"): the first live test call lost its first 20 s that way. A 100 ms frame
    counts as loud when its RMS is at least 30% of the loud-end (90th percentile) frame
    RMS; a run of >= min_frames loud frames not covered by any VAD segment is returned as
    an extra (start_sample, samples) segment, padded by 200 ms each side."""
    n = len(samples) // frame
    if n == 0:
        return []
    frames = np.asarray(samples[: n * frame], dtype=np.float32).reshape(n, frame)
    energy = np.sqrt(np.mean(frames * frames, axis=1))
    loud_level = float(np.percentile(energy, 90))
    if loud_level <= 0:
        return []
    loud = energy >= 0.3 * loud_level
    covered = np.zeros(n, dtype=bool)
    for start, seg in segments:
        first = int(start) // frame
        last = (int(start) + len(seg) + frame - 1) // frame
        covered[max(first, 0):min(last, n)] = True
    extra = []
    i = 0
    while i < n:
        if loud[i] and not covered[i]:
            j = i
            while j < n and loud[j] and not covered[j]:
                j += 1
            if j - i >= min_frames:
                a = max(i * frame - pad, 0)
                b = min(j * frame + pad, len(samples), a + max_len)
                extra.append((a, samples[a:b]))
            i = j
        else:
            i += 1
    return extra


def _decode_segments(recognizer, segments, punct):
    texts = []
    batch_size = 8
    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        streams = []
        for _, samples in batch:
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            streams.append(stream)
        recognizer.decode_streams(streams)
        for stream in streams:
            raw = stream.result.text.strip()
            texts.append(clean_text(raw, punct))
    return texts


class LockedPunct:
    """The punctuation model is shared by every request; one call at a time (it is fast)."""

    def __init__(self, punct):
        self._punct = punct
        self._lock = threading.Lock()

    def add_punctuation_with_case(self, text):
        with self._lock:
            return self._punct.add_punctuation_with_case(text)


def load_models():
    import sherpa_onnx

    models_dir = os.environ.get("MODELS_DIR", "/models")
    threads = int(os.environ.get("STT_THREADS", "2"))
    engines = {}

    parakeet_dir = os.path.join(models_dir, "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
    if os.path.isdir(parakeet_dir):
        try:
            recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(parakeet_dir, "encoder.int8.onnx"),
                decoder=os.path.join(parakeet_dir, "decoder.int8.onnx"),
                joiner=os.path.join(parakeet_dir, "joiner.int8.onnx"),
                tokens=os.path.join(parakeet_dir, "tokens.txt"),
                num_threads=threads,
                model_type="nemo_transducer",
            )
            engines["parakeet"] = recognizer
        except Exception as exc:
            print(f"warning: failed to load parakeet model: {type(exc).__name__}", file=sys.stderr)
    else:
        print(f"warning: missing parakeet model directory: {parakeet_dir}", file=sys.stderr)

    zipformer_dir = os.path.join(models_dir, "sherpa-onnx-zipformer-gigaspeech-2023-12-12")
    if os.path.isdir(zipformer_dir):
        try:
            recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(zipformer_dir, "encoder-epoch-30-avg-1.int8.onnx"),
                decoder=os.path.join(zipformer_dir, "decoder-epoch-30-avg-1.onnx"),
                joiner=os.path.join(zipformer_dir, "joiner-epoch-30-avg-1.int8.onnx"),
                tokens=os.path.join(zipformer_dir, "tokens.txt"),
                num_threads=threads,
            )
            engines["zipformer"] = recognizer
            # A second instance only for live captions: a recognizer is
            # never shared between a long after-call job and a live sentence.
            engines["zipformer-live"] = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(zipformer_dir, "encoder-epoch-30-avg-1.int8.onnx"),
                decoder=os.path.join(zipformer_dir, "decoder-epoch-30-avg-1.onnx"),
                joiner=os.path.join(zipformer_dir, "joiner-epoch-30-avg-1.int8.onnx"),
                tokens=os.path.join(zipformer_dir, "tokens.txt"),
                num_threads=int(os.environ.get("STT_LIVE_THREADS", "2")),
            )
        except Exception as exc:
            print(f"warning: failed to load zipformer model: {type(exc).__name__}", file=sys.stderr)
    else:
        print(f"warning: missing zipformer model directory: {zipformer_dir}", file=sys.stderr)

    punct = None
    punct_dir = os.path.join(models_dir, "sherpa-onnx-online-punct-en-2024-08-06")
    if os.path.isdir(punct_dir) and "zipformer" in engines:
        try:
            punct = sherpa_onnx.OnlinePunctuation(
                sherpa_onnx.OnlinePunctuationConfig(
                    model_config=sherpa_onnx.OnlinePunctuationModelConfig(
                        cnn_bilstm=os.path.join(punct_dir, "model.int8.onnx"),
                        bpe_vocab=os.path.join(punct_dir, "bpe.vocab"),
                        num_threads=1,
                    )
                )
            )
        except Exception as exc:
            print(f"warning: failed to load punctuation model: {type(exc).__name__}", file=sys.stderr)

    vad_path = os.path.join(models_dir, "silero_vad.onnx")
    if not os.path.exists(vad_path):
        print(f"warning: missing VAD model: {vad_path}", file=sys.stderr)
        vad_path = None

    return engines, punct, vad_path


def make_server(port, token, engines, punct, vad_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SttWorker/1.0"

        def log_message(self, format, *args):
            pass

        def _send_json(self, status, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorize(self):
            header = self.headers.get("Authorization")
            if not authorized(header, token):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self._authorize():
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._health()
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorize():
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/transcribe":
                self._transcribe()
            else:
                self._send_json(404, {"error": "not found"})

        def _health(self):
            with self.server.active_lock:
                busy = self.server.active
            self._send_json(200, {
                "ok": True,
                "busy": busy,
                "capacity": self.server.capacity,
                "live_busy": self.server.live_active,
                "live_capacity": self.server.live_capacity,
                "engines": sorted(self.server.engines.keys()),
                "cpus": os.cpu_count(),
                "load1": os.getloadavg()[0],
            })

        def _transcribe(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            engine = qs.get("engine", [None])[0]
            # Live captions (one sentence at a time) have their own slots, so a long
            # after-call job never makes a caption wait.
            live = qs.get("live", ["0"])[0] == "1"
            if live and "zipformer-live" in self.server.engines:
                engine = "zipformer-live"
            semaphore = self.server.live_semaphore if live else self.server.semaphore
            counter = "live_active" if live else "active"

            if engine not in self.server.engines:
                self._send_json(400, {"error": "unknown or unavailable engine"})
                return

            if not semaphore.acquire(blocking=False):
                self._send_json(503, {"error": "busy"})
                return

            with self.server.active_lock:
                setattr(self.server, counter, getattr(self.server, counter) + 1)

            tmp_path = None
            started = time.monotonic()
            self_before = resource.getrusage(resource.RUSAGE_SELF)
            children_before = resource.getrusage(resource.RUSAGE_CHILDREN)

            try:
                length_header = self.headers.get("Content-Length")
                if not length_header or not length_header.isdigit():
                    self._send_json(413, {"error": "content length required"})
                    return
                length = int(length_header)
                if length <= 0 or length > MAX_BODY:
                    self._send_json(413, {"error": "payload too large"})
                    return

                body = self.rfile.read(length)
                if len(body) != length:
                    self._send_json(400, {"error": "bad request"})
                    return

                fd, tmp_path = tempfile.mkstemp(prefix="stt-", suffix=".audio")
                with os.fdopen(fd, "wb") as f:
                    f.write(body)

                channels = probe_channels(tmp_path)
                audio_sec = 0.0
                all_segments = []

                for c in range(channels):
                    samples = decode_channel(tmp_path, c)
                    if c == 0:
                        audio_sec = len(samples) / SAMPLE_RATE
                    if rms(samples) < SILENCE_RMS:
                        continue

                    segments = speech_segments(samples, self.server.vad_path)
                    if not segments:
                        continue

                    use_punct = (
                        self.server.punct if engine in ("zipformer", "zipformer-live") else None
                    )
                    texts = _decode_segments(self.server.engines[engine], segments, use_punct)

                    pieces = []
                    for (start_sample, seg_samples), text in zip(segments, texts):
                        pieces.append((start_sample, len(seg_samples), text))
                    all_segments.extend(to_segments(c, pieces))

                all_segments.sort(key=lambda s: (s["start_ms"], s["channel"]))

                self_after = resource.getrusage(resource.RUSAGE_SELF)
                children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                cpu_sec = (
                    (self_after.ru_utime - self_before.ru_utime)
                    + (self_after.ru_stime - self_before.ru_stime)
                    + (children_after.ru_utime - children_before.ru_utime)
                    + (children_after.ru_stime - children_before.ru_stime)
                )
                wall_sec = time.monotonic() - started
                n_segments = len(all_segments)

                print(json.dumps({
                    "engine": engine,
                    "channels": channels,
                    "audio_sec": round(audio_sec, 3),
                    "cpu_sec": round(cpu_sec, 3),
                    "wall_sec": round(wall_sec, 3),
                    "n_segments": n_segments,
                }), flush=True)

                self._send_json(200, {
                    "engine": engine,
                    "channels": channels,
                    "audio_sec": audio_sec,
                    "cpu_sec": cpu_sec,
                    "wall_sec": wall_sec,
                    "segments": all_segments,
                })

            except AudioDecodeError:
                self._send_json(422, {"error": "undecodable audio"})
            except Exception as exc:
                print(f"error: {type(exc).__name__}", file=sys.stderr, flush=True)
                self._send_json(500, {"error": "internal"})
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                with self.server.active_lock:
                    setattr(self.server, counter, getattr(self.server, counter) - 1)
                semaphore.release()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.engines = engines
    server.punct = LockedPunct(punct) if punct is not None else None
    server.vad_path = vad_path
    server.capacity = int(os.environ.get("STT_CONCURRENCY", "1"))
    server.semaphore = threading.BoundedSemaphore(server.capacity)
    server.active = 0
    server.live_capacity = int(os.environ.get("STT_LIVE_CONCURRENCY", "2"))
    server.live_semaphore = threading.BoundedSemaphore(server.live_capacity)
    server.live_active = 0
    server.active_lock = threading.Lock()
    return server


def main():
    secret = os.environ.get("LIVEKIT_API_SECRET")
    if not secret:
        raise SystemExit("LIVEKIT_API_SECRET is required")
    token = auth_token(secret)
    engines, punct, vad_path = load_models()
    port = int(os.environ.get("PORT", "9100"))
    server = make_server(port, token, engines, punct, vad_path)
    print(f"stt worker listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
