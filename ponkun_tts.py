"""Shared text-to-speech adapter for Ponkun PC and Mobile.

The default backend remains the historical localhost service. Cloud mode is
explicitly opt-in through PONKUN_TTS_BACKEND=fish_cloud.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

import requests


LEGACY_LOCAL = "legacy_local"
FISH_CLOUD = "fish_cloud"
DEFAULT_LEGACY_URL = "http://localhost:8000/v1/audio/speech"
DEFAULT_FISH_URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2-pro"
DEFAULT_REFERENCE_ID = "5116d0b0e7f64901be4a086b7347ad2e"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MIN_AUDIO_DURATION_SECONDS = 0.25


class TTSError(RuntimeError):
    """Base class for TTS configuration, transport, and API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class TTSConfigurationError(TTSError):
    pass


class TTSAPIError(TTSError):
    pass


@dataclass(frozen=True)
class TTSResult:
    audio: bytes
    content_type: str
    elapsed_ms: int
    backend: str
    status_code: int
    duration_seconds: Optional[float] = None


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _response_error(response) -> str:
    # Never include response bodies: a provider could echo request data or
    # return sensitive diagnostics. Status is sufficient for callers/logs.
    return f"Fish Audio TTS HTTP {response.status_code}"


def _skip_id3v2(data: bytes) -> int:
    if not data.startswith(b"ID3"):
        return 0
    if len(data) < 10:
        raise TTSAPIError("TTS returned a truncated ID3 header")
    size_bytes = data[6:10]
    if any(value & 0x80 for value in size_bytes):
        raise TTSAPIError("TTS returned an invalid ID3 header")
    tag_size = (
        (size_bytes[0] << 21)
        | (size_bytes[1] << 14)
        | (size_bytes[2] << 7)
        | size_bytes[3]
    )
    footer_size = 10 if data[5] & 0x10 else 0
    end = 10 + tag_size + footer_size
    if end > len(data):
        raise TTSAPIError("TTS returned a truncated ID3 tag")
    return end


def _mp3_duration_seconds(data: bytes) -> float:
    """Return duration for complete MP3 frames, rejecting truncation."""

    bitrate_tables = {
        3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
        2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
        0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    }
    sample_rates = {
        3: (44100, 48000, 32000),
        2: (22050, 24000, 16000),
        0: (11025, 12000, 8000),
    }
    offset = _skip_id3v2(data)
    frames = 0
    duration = 0.0
    while offset + 4 <= len(data):
        if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
            break
        header = int.from_bytes(data[offset:offset + 4], "big")
        version = (header >> 19) & 0x3
        layer = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0x3
        padding = (header >> 9) & 0x1
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_index == 3:
            break
        bitrate = bitrate_tables[version][bitrate_index] * 1000
        sample_rate = sample_rates[version][sample_index]
        frame_length = ((144 if version == 3 else 72) * bitrate // sample_rate) + padding
        if frame_length <= 4 or offset + frame_length > len(data):
            break
        frames += 1
        duration += (1152 if version == 3 else 576) / sample_rate
        offset += frame_length

    trailing = data[offset:]
    if trailing and not (trailing.startswith(b"TAG") and len(trailing) == 128):
        raise TTSAPIError("TTS returned an incomplete MP3 frame")
    if frames < 3:
        raise TTSAPIError("TTS returned too few MP3 frames")
    return duration


def _validate_audio_response(
    response,
    backend: str,
    started: float,
    expected_format: str,
    min_duration_seconds: float,
) -> TTSResult:
    content = response.content or b""
    if not content:
        raise TTSAPIError("TTS returned an empty audio response", response.status_code)

    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
        raise TTSAPIError("TTS returned a non-audio content type", response.status_code)

    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) != len(content):
        raise TTSAPIError("TTS response Content-Length does not match received audio", response.status_code)

    duration_seconds = None
    if expected_format.lower() == "mp3":
        if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
            raise TTSAPIError("TTS MP3 response has an unexpected content type", response.status_code)
        duration_seconds = _mp3_duration_seconds(content)
        if duration_seconds < min_duration_seconds:
            raise TTSAPIError("TTS MP3 response is shorter than the minimum duration", response.status_code)

    return TTSResult(
        audio=content,
        content_type=content_type,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        backend=backend,
        status_code=response.status_code,
        duration_seconds=duration_seconds,
    )


def synthesize(
    text: str,
    *,
    backend: Optional[str] = None,
    request_fn: Optional[Callable] = None,
    timeout: Optional[float] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> TTSResult:
    """Synthesize one text turn without retries.

    `fish_cloud` is opt-in and sends the supplied text outside the machine.
    The default `legacy_local` backend never needs a Fish API key.
    """

    env = environ if environ is not None else os.environ
    selected = (backend or env.get("PONKUN_TTS_BACKEND", LEGACY_LOCAL)).strip().lower()
    if selected not in {LEGACY_LOCAL, FISH_CLOUD}:
        raise TTSConfigurationError(f"Unsupported TTS backend: {selected}")

    clean_text = (text or "").strip()
    if not clean_text:
        raise TTSConfigurationError("TTS text is empty")

    model = env.get("PONKUN_TTS_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    reference_id = env.get("PONKUN_TTS_REFERENCE_ID", DEFAULT_REFERENCE_ID).strip()
    timeout_seconds = float(timeout if timeout is not None else env.get(
        "PONKUN_TTS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
    ))
    expected_format = env.get("PONKUN_TTS_FORMAT", "mp3").strip().lower() or "mp3"
    min_duration_seconds = float(env.get(
        "PONKUN_TTS_MIN_AUDIO_DURATION_SECONDS",
        str(DEFAULT_MIN_AUDIO_DURATION_SECONDS),
    ))
    post = request_fn or requests.post
    started = time.perf_counter()

    if selected == LEGACY_LOCAL:
        url = env.get("PONKUN_TTS_LEGACY_URL", DEFAULT_LEGACY_URL).strip()
        payload = {
            "model": model,
            "voice": reference_id,
            "input": clean_text,
            "response_format": expected_format,
        }
        headers = {"Accept": "audio/*"}
    else:
        key_env = env.get("PONKUN_TTS_API_KEY_ENV", "FISH_API_KEY").strip() or "FISH_API_KEY"
        api_key = env.get(key_env, "").strip()
        if not api_key:
            raise TTSConfigurationError(f"Cloud TTS key is not configured in {key_env}")
        url = env.get("PONKUN_TTS_FISH_URL", DEFAULT_FISH_URL).strip()
        payload = {
            "text": clean_text,
            "reference_id": reference_id,
            "format": expected_format,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/*",
            "model": model,
        }

    try:
        response = post(url, json=payload, headers=headers, timeout=timeout_seconds)
    except requests.Timeout as exc:
        raise TTSError("TTS request timed out") from exc
    except requests.RequestException as exc:
        raise TTSError("TTS request failed") from exc

    if response.status_code >= 400:
        raise TTSAPIError(_response_error(response), response.status_code)
    return _validate_audio_response(
        response,
        selected,
        started,
        expected_format,
        min_duration_seconds,
    )
