"""Opt-in OpenAI Responses API image analysis adapter.

The adapter is disabled by default, reads the OpenAI credential only when a
caller has already obtained per-image consent, and performs one request with
no automatic retry or redirect-following.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

try:
    from ponkun_hub.secure_credentials import read_openai_key
except ImportError:
    read_openai_key = None


class OpenAIVisionError(RuntimeError):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.stage = "vision.provider"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _read_key() -> str:
    value = os.environ.get("PONKUN_OPENAI_API_KEY", "").strip()
    if not value and read_openai_key is not None:
        try:
            value = read_openai_key() or ""
        except Exception as exc:
            raise OpenAIVisionError("vision_credential_invalid") from exc
    if not value:
        raise OpenAIVisionError("vision_credential_missing")
    return value


def _extract_text(payload: dict) -> str:
    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"].strip())
    answer = "\n".join(part for part in parts if part).strip()
    if not answer:
        raise OpenAIVisionError("vision_empty_response")
    return answer


def analyze_image(text: str, image_bytes: bytes, mime_type: str) -> tuple[str, dict]:
    """Analyze one in-memory image after explicit user consent.

    The raw image is never written to disk. The returned metadata contains no
    credential, image content, prompt, or provider response body.
    """
    if os.environ.get("PONKUN_VISION_BACKEND", "disabled").strip().lower() != "openai":
        raise OpenAIVisionError("vision_disabled", 503)
    if not image_bytes:
        raise OpenAIVisionError("vision_empty_image", 400)
    # The official image guide lists PNG, JPEG, WEBP, and non-animated GIF.
    # Mobile's existing validator admits PNG/JPEG/WEBP; GIF remains excluded.
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise OpenAIVisionError("vision_unsupported_format", 415)

    model = os.environ.get("PONKUN_VISION_MODEL", "gpt-4.1-mini").strip()
    payload = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": text.strip() or "画像を日本語で説明してください。"},
                {"type": "input_image", "image_url": "data:" + mime_type + ";base64," + base64.b64encode(image_bytes).decode("ascii"), "detail": "auto"},
            ],
        }],
        "max_output_tokens": int(os.environ.get("PONKUN_VISION_MAX_OUTPUT_TOKENS", "500")),
    }
    key = _read_key()
    request = urllib.request.Request(
        os.environ.get("PONKUN_VISION_ENDPOINT", "https://api.openai.com/v1/responses"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        # No retry and no redirect following: a single explicit provider call.
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=float(os.environ.get("PONKUN_VISION_TIMEOUT", "30"))) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read()
        parsed = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = {401: "vision_authentication_failed", 403: "vision_permission_denied", 408: "vision_timeout", 429: "vision_rate_limited", 402: "vision_billing_or_balance"}.get(exc.code, "vision_provider_error")
        raise OpenAIVisionError(code, exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OpenAIVisionError("vision_network_or_timeout") from exc
    except (UnicodeError, ValueError, TypeError) as exc:
        raise OpenAIVisionError("vision_invalid_response") from exc
    answer = _extract_text(parsed)
    return answer, {"provider": "openai", "model": model, "status": status}
