"""One-shot, secret-safe Strata API credential diagnostic.

This module reads the existing Windows Credential Manager entry in memory and
performs exactly one GET request to /v1/models. It never prints, hashes, or
writes the credential or response body.
"""
from __future__ import annotations

import argparse
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

try:
    from .secure_credentials import CredentialError, read_strata_key
except ImportError:  # pragma: no cover
    from ponkun_hub.secure_credentials import CredentialError, read_strata_key


DEFAULT_URL = "http://127.0.0.1:47831/v1/models"
TARGET_MODEL = "iq3_s"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class DiagnosticResult:
    credential_status: str
    http_status: str
    authentication: str
    target_model: str
    detail: str


def _classify_status(status: int) -> tuple[str, str]:
    if status == 200:
        return "accepted", "authenticated request succeeded"
    if status in (401, 403):
        return "rejected", "authentication rejected"
    if status == 404:
        return "endpoint-not-found", "endpoint not found"
    if status in (408, 504):
        return "timeout", "request timed out"
    if 500 <= status <= 599:
        return "server-error", "Strata returned a server error"
    return "unexpected", "unexpected HTTP status"


def run_once(
    *,
    url: str = DEFAULT_URL,
    key_reader: Callable[[], str | None] = read_strata_key,
    opener=None,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """Read the credential and make one request, without exposing secrets."""
    try:
        key = key_reader()
    except CredentialError:
        return DiagnosticResult("present-invalid", "not-sent", "not-tested", "not-tested", "credential invalid")
    except Exception:
        return DiagnosticResult("unavailable", "not-sent", "not-tested", "not-tested", "credential read failed")
    if not key:
        return DiagnosticResult("missing", "not-sent", "not-tested", "not-tested", "credential missing")

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}, method="GET")
    http_opener = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with http_opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        authentication, detail = _classify_status(status)
        return DiagnosticResult("present-valid", str(status), authentication, "unknown", detail)
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return DiagnosticResult("present-valid", "connection-failed", "not-tested", "unknown", "connection or timeout failure")
    except OSError:
        return DiagnosticResult("present-valid", "connection-failed", "not-tested", "unknown", "connection failure")

    if status != 200:
        authentication, detail = _classify_status(status)
        return DiagnosticResult("present-valid", str(status), authentication, "unknown", detail)
    try:
        data = json.loads(body.decode("utf-8"))
        ids = {item.get("id") for item in data.get("data", []) if isinstance(item, dict)}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        return DiagnosticResult("present-valid", "200", "accepted", "unknown", "response JSON invalid")
    return DiagnosticResult(
        "present-valid", "200", "accepted", "present" if TARGET_MODEL in ids else "absent", "models response parsed"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one secret-safe Strata /v1/models check")
    parser.add_argument("--run-once", action="store_true", help="perform exactly one request")
    args = parser.parse_args()
    if not args.run_once:
        parser.error("--run-once is required")
    result = run_once()
    print(f"credential_status={result.credential_status}")
    print(f"http_status={result.http_status}")
    print(f"authentication={result.authentication}")
    print(f"iq3_s={result.target_model}")
    print(f"detail={result.detail}")
    return 0 if result.authentication == "accepted" and result.target_model == "present" else 1


if __name__ == "__main__":
    raise SystemExit(main())
