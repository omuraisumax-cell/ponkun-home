"""Fish Audio Cloud proxy with file-based credentials and sanitized failures.

The credential is read only from a mounted secret file.  It is never accepted
in the request body, returned in a response, or included in an exception.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field


FISH_TTS_URL = "https://api.fish.audio/v1/tts"
DEFAULT_SECRET_FILE = "/run/ponkun-secrets/fish_audio_api_key"


class SpeechRequest(BaseModel):
    model: str = Field(min_length=1)
    input: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    response_format: str = Field(min_length=1)


class ProxyConfigurationError(RuntimeError):
    pass


def read_api_key_file(path: str | os.PathLike[str]) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ProxyConfigurationError("Fish Audio credential is unavailable") from exc
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProxyConfigurationError("Fish Audio credential is invalid") from exc
    if (
        raw.startswith(b"\xef\xbb\xbf")
        or value.encode("utf-8") != raw
        or not 20 <= len(value) <= 512
        or not value.isascii()
        or any(char.isspace() or ord(char) < 33 for char in value)
    ):
        raise ProxyConfigurationError("Fish Audio credential is invalid")
    return value


def classify_upstream_status(status: int) -> tuple[int, str]:
    if status in (401, 403):
        return 502, "upstream_auth_failed"
    if status == 400:
        return 502, "upstream_bad_request"
    if status == 422:
        return 502, "upstream_validation_failed"
    if status == 429:
        return 503, "upstream_rate_limited"
    if status >= 500:
        return 502, "upstream_unavailable"
    return 502, "upstream_request_failed"


def create_app(
    *,
    secret_reader: Callable[[str], str] = read_api_key_file,
    post: Callable[..., object] | None = None,
):
    """Create the app; missing credentials abort before the server can bind."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response
    import requests

    secret_path = os.environ.get("FISH_AUDIO_API_KEY_FILE", DEFAULT_SECRET_FILE)
    api_key = secret_reader(secret_path)
    request_post = post or requests.post
    app = FastAPI(title="Ponkun Fish Audio Cloud Proxy")

    @app.exception_handler(RequestValidationError)
    async def local_validation_error(_request: Request, _exc: RequestValidationError):
        # Never echo validation details because they can contain speech text.
        return JSONResponse(
            status_code=422,
            content={"detail": "local_request_validation_failed"},
            headers={"X-Ponkun-Validation-Origin": "local"},
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "route": "fish_cloud", "credential": "configured"}

    @app.post("/v1/audio/speech")
    def proxy_speech(body: SpeechRequest):
        payload = {
            "text": body.input,
            "reference_id": body.voice,
            "format": body.response_format,
        }
        try:
            upstream = request_post(
                FISH_TTS_URL,
                json=payload,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/*",
                    "model": body.model,
                },
                timeout=(5, 120),
            )
        except (requests.Timeout, TimeoutError) as exc:
            raise HTTPException(504, "upstream_timeout") from exc
        except requests.RequestException as exc:
            raise HTTPException(502, "upstream_connection_failed") from exc
        if upstream.status_code != 200:
            public_status, public_code = classify_upstream_status(upstream.status_code)
            # The numeric status is safe diagnostic metadata.  No upstream
            # body or headers are forwarded.
            raise HTTPException(
                public_status,
                public_code,
                headers={"X-Ponkun-Upstream-Status": str(upstream.status_code)},
            )
        return Response(
            content=upstream.content,
            media_type=upstream.headers.get("content-type", "audio/mpeg"),
        )

    return app


# Importing this module in the container deliberately fails closed when the
# mounted secret is absent or invalid.
app = create_app()
