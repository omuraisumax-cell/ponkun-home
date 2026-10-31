"""Optional shared inference adapter for Ponkun clients.

The adapter is disabled by default.  When enabled, it talks only to an
explicitly configured OpenAI-compatible HTTP endpoint and never starts or
stops that endpoint.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from ponkun_hub.secure_credentials import CredentialError, read_strata_key
except ImportError:
    CredentialError = RuntimeError
    read_strata_key = None


BACKEND = os.environ.get("PONKUN_INFERENCE_BACKEND", "openai").strip().lower()
STRATA_URL = os.environ.get("PONKUN_STRATA_URL", "").strip().rstrip("/")
STRATA_MODEL = os.environ.get("PONKUN_STRATA_MODEL", "qwen2.5:32b").strip()
STRATA_API_KEY = os.environ.get("PONKUN_STRATA_API_KEY", "").strip()
STRATA_CREDENTIAL_STATUS = "environment" if STRATA_API_KEY else "not-configured"
if BACKEND == "strata" and not STRATA_API_KEY and read_strata_key is not None:
    try:
        stored_key = read_strata_key()
        if stored_key:
            STRATA_API_KEY = stored_key
            STRATA_CREDENTIAL_STATUS = "credential-manager"
        else:
            STRATA_CREDENTIAL_STATUS = "missing"
    except CredentialError:
        STRATA_CREDENTIAL_STATUS = "invalid"
HUB_URL = os.environ.get("PONKUN_HUB_URL", "").strip().rstrip("/")
HUB_CLIENT_TOKEN = os.environ.get("PONKUN_HUB_CLIENT_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("PONKUN_STRATA_TIMEOUT", "60"))
MAX_CONCURRENCY = max(1, int(os.environ.get("PONKUN_STRATA_MAX_CONCURRENCY", "1")))
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
_last_hub_inference_status: dict = {}
INFERENCE_DIAGNOSTIC_LOG = Path(os.environ.get(
    "PONKUN_INFERENCE_DIAGNOSTIC_LOG",
    str(Path(__file__).resolve().parent / "ponkun_hub" / "runtime_logs" / "inference_errors.log"),
))


class InferenceUnavailable(RuntimeError):
    """Raised when the explicitly configured shared backend cannot answer."""

    def __init__(self, message: str, *, code: str = "inference_unavailable", http_status: int | None = None, stage: str = "inference"):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.stage = stage


def _write_inference_diagnostic(code: str, http_status: int | None, stage: str, exception_class: str) -> None:
    """Write only non-secret transport metadata for post-failure diagnosis."""
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "code": code,
        "http_status": http_status,
        "stage": stage,
        "exception_class": exception_class,
    }
    try:
        INFERENCE_DIAGNOSTIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with INFERENCE_DIAGNOSTIC_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _validate_http_headers(headers: dict[str, str]) -> None:
    """Reject values urllib/http.client cannot safely encode.

    HTTP header values are encoded as Latin-1 by the standard library. Do not
    include the offending value in the exception: Authorization and client
    token values are secrets.
    """
    for name, value in headers.items():
        try:
            name.encode("ascii")
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            lowered = name.lower()
            if lowered == "authorization":
                raise InferenceUnavailable(
                    "Strata API key contains characters unsupported in HTTP headers; "
                    "check that only the raw key was supplied"
                ) from exc
            if lowered == "x-ponkun-client-token":
                raise InferenceUnavailable(
                    "Hub client token contains characters unsupported in HTTP headers"
                ) from exc
            raise InferenceUnavailable(
                f"HTTP header {name!r} contains characters unsupported in HTTP headers"
            ) from exc


def is_enabled() -> bool:
    return BACKEND == "strata" and bool(STRATA_URL)


def status() -> dict:
    return {
        "backend": "strata" if is_enabled() else BACKEND,
        "configured": bool(STRATA_URL) if BACKEND == "strata" else True,
        "endpoint": STRATA_URL if is_enabled() else None,
        "model": STRATA_MODEL if is_enabled() else None,
        "authentication_configured": bool(STRATA_API_KEY) if is_enabled() else False,
        "credential_status": STRATA_CREDENTIAL_STATUS if is_enabled() else "not-applicable",
        "hub_configured": bool(HUB_URL),
        "max_concurrency": MAX_CONCURRENCY,
    }


def hub_enabled() -> bool:
    return bool(HUB_URL)


def hub_model_name() -> str:
    """Return the last model reported by Hub without exposing credentials."""
    return str(_last_hub_inference_status.get("model") or STRATA_MODEL or "unknown")


def _endpoint() -> str:
    if not is_enabled():
        raise InferenceUnavailable("shared Strata backend is not configured")
    if STRATA_URL.endswith("/chat/completions"):
        return STRATA_URL
    return STRATA_URL + "/v1/chat/completions"


def chat(model: str, instructions: str, user_text: str) -> str:
    payload = {
        "model": STRATA_MODEL or model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {STRATA_API_KEY}"} if STRATA_API_KEY else {}),
    }
    _validate_http_headers(headers)
    request = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    acquired = _slots.acquire(timeout=TIMEOUT)
    if not acquired:
        raise InferenceUnavailable("shared inference queue is full", code="inference_queue_full", http_status=503, stage="strata.queue")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = f"strata_http_{exc.code}"
        _write_inference_diagnostic(code, exc.code, "strata.http", type(exc).__name__)
        raise InferenceUnavailable("Strata inference HTTP request failed", code=code, http_status=exc.code, stage="strata.http") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        code = "strata_timeout" if isinstance(exc, TimeoutError) or "timed out" in str(exc).casefold() else "strata_connection_error"
        status = 504 if code == "strata_timeout" else 502
        _write_inference_diagnostic(code, status, "strata.connect", type(exc).__name__)
        raise InferenceUnavailable("Strata inference connection failed", code=code, http_status=status, stage="strata.connect") from exc
    except ValueError as exc:
        _write_inference_diagnostic("strata_invalid_response", 502, "strata.response", type(exc).__name__)
        raise InferenceUnavailable("Strata inference response was invalid", code="strata_invalid_response", http_status=502, stage="strata.response") from exc
    finally:
        _slots.release()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InferenceUnavailable("shared inference response is not OpenAI-compatible", code="strata_invalid_response", http_status=502, stage="strata.response") from exc
    if not isinstance(content, str) or not content.strip():
        raise InferenceUnavailable("shared inference returned empty content", code="strata_empty_response", http_status=502, stage="strata.response")
    return content.strip()


def hub_chat(model: str, instructions: str, user_text: str) -> str:
    """Use the local Hub as the shared inference hop for PC and Mobile."""
    if not hub_enabled():
        raise InferenceUnavailable("shared Hub backend is not configured")
    payload = {
        "model": model,
        "instructions": instructions,
        "user_text": user_text,
    }
    headers = {"Content-Type": "application/json"}
    if HUB_CLIENT_TOKEN:
        headers["X-Ponkun-Client-Token"] = HUB_CLIENT_TOKEN
    _validate_http_headers(headers)
    request = urllib.request.Request(
        HUB_URL + "/api/inference",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = f"hub_http_{exc.code}"
        _write_inference_diagnostic(code, exc.code, "hub.http", type(exc).__name__)
        raise InferenceUnavailable("Hub inference HTTP request failed", code=code, http_status=exc.code, stage="hub.http") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).casefold()
        code = "hub_timeout" if is_timeout else "hub_connection_error"
        status = 504 if is_timeout else 502
        _write_inference_diagnostic(code, status, "hub.connect", type(exc).__name__)
        raise InferenceUnavailable("Hub inference connection failed", code=code, http_status=status, stage="hub.connect") from exc
    except ValueError as exc:
        _write_inference_diagnostic("hub_invalid_response", 502, "hub.response", type(exc).__name__)
        raise InferenceUnavailable("Hub inference response was invalid", code="hub_invalid_response", http_status=502, stage="hub.response") from exc
    global _last_hub_inference_status
    _last_hub_inference_status = data.get("inference") if isinstance(data.get("inference"), dict) else {}
    content = data.get("text")
    if not isinstance(content, str) or not content.strip():
        raise InferenceUnavailable("Hub inference returned empty content", code="hub_empty_response", http_status=502, stage="hub.response")
    print(f"[ponkun-timing] hub_inference_ms={round((time.perf_counter() - started) * 1000)}")
    return content.strip()
