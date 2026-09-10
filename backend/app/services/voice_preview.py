"""Preview audio for an assistant voice, cached in the media store."""

from __future__ import annotations

import hashlib

import httpx

from app.errors import ValidationFailedError
from app.services import ai_providers as ai_providers_svc

# PREVIEW_MAX_CHARS is a TEXT cap, not a duration cap: we cannot measure the length of
# audio we have not decoded, and 140 characters of speech is comfortably around three
# seconds at normal speed.
PREVIEW_MAX_CHARS = 140
DEFAULT_PREVIEW_TEXT = "Hi, thanks for calling. How can I help you today?"
PREVIEW_TIMEOUT_SECONDS = 20.0
PREVIEW_CONTENT_TYPE = "audio/mpeg"


def cache_key(org_id, *, provider: str, voice_id: str, model: str, text: str) -> str:
    """org/{org_id}/voice-preview/{provider}/{sha256 of provider|voice|model|text}.mp3"""
    digest = hashlib.sha256(
        "|".join((provider, voice_id, model, text)).encode("utf-8")
    ).hexdigest()
    return f"org/{org_id}/voice-preview/{provider}/{digest}.mp3"


def _scrub_detail(detail: str, api_key: str) -> str:
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    return detail


async def synthesize(provider, *, api_key, voice_id, model, text, client=None) -> bytes:
    if provider == "elevenlabs":
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        json_body = {"text": text, "model_id": model or "eleven_turbo_v2_5"}
    elif provider == "cartesia":
        url = "https://api.cartesia.ai/tts/bytes"
        headers = {
            "X-API-Key": api_key,
            "Cartesia-Version": "2024-06-10",
            "content-type": "application/json",
        }
        json_body = {
            "model_id": model or "sonic-english",
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
        }
    else:
        raise ValidationFailedError("We cannot preview that voice here yet.")

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=PREVIEW_TIMEOUT_SECONDS)
    try:
        response = await http.post(
            url, headers=headers, json=json_body, timeout=PREVIEW_TIMEOUT_SECONDS
        )
        if not 200 <= response.status_code < 300:
            provider_detail = _scrub_detail(response.text[:200], api_key)
            raise ValidationFailedError("We could not preview that voice. " + provider_detail)
        return response.content
    except httpx.RequestError as exc:
        detail = _scrub_detail(str(exc), api_key)
        raise ValidationFailedError(
            "We could not reach the voice provider. " + detail[:200]
        ) from exc
    finally:
        if owns_client:
            await http.aclose()


async def preview(
    session,
    settings,
    store,
    *,
    org,
    profile,
    tts_provider,
    voice_id,
    text,
    client=None,
) -> bytes:
    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=True
    )
    resolved_provider = cfg["tts"]["provider"]
    provider = tts_provider or resolved_provider
    # The key resolved above belongs to `resolved_provider`. Sending it to a DIFFERENT
    # provider the caller named would hand one vendor another vendor's key - the same
    # mistake D47 fixed in resolve_call_config. Refuse instead.
    if tts_provider and resolved_provider and tts_provider != resolved_provider:
        raise ValidationFailedError(
            "That voice provider is not connected for this organization yet."
        )
    model = ai_providers_svc.DEFAULT_TTS_MODELS.get(provider, "")
    key = (cfg.get("keys") or {}).get("tts")
    if not key:
        raise ValidationFailedError("This assistant has no working voice connection yet.")
    resolved_voice_id = voice_id or cfg["tts"]["voice_id"]
    if not resolved_voice_id:
        raise ValidationFailedError("Pick a voice first.")
    text_clean = (text or DEFAULT_PREVIEW_TEXT).strip()[:PREVIEW_MAX_CHARS]
    key_cache = cache_key(
        org.id, provider=provider, voice_id=resolved_voice_id, model=model, text=text_clean
    )
    if await store.exists(key_cache):
        return await store.get(key_cache)
    audio = await synthesize(
        provider,
        api_key=key,
        voice_id=resolved_voice_id,
        model=model,
        text=text_clean,
        client=client,
    )
    await store.put(key_cache, audio, PREVIEW_CONTENT_TYPE)
    return audio
