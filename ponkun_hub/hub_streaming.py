"""Independent Hub-side Strata SSE adapter; not wired into the live server."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, Optional

from ponkun_hub.streaming_inference import (
    SSEDecoder,
    SSEParseError,
    StreamDisconnected,
    StreamTimeout,
)


class HubStreamingError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class HubStreamConfig:
    endpoint: str = "http://127.0.0.1:47831/v1/chat/completions"
    model: str = "iq3_s"
    timeout: float = 90.0
    chunk_size: int = 256


@dataclass(frozen=True)
class HubStreamResult:
    text: str
    deltas: tuple[str, ...]


def build_strata_request(
    config: HubStreamConfig,
    instructions: str,
    user_text: str,
    api_key: str,
) -> urllib.request.Request:
    payload = json.dumps({
        "model": config.model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_text},
        ],
        "stream": True,
    }).encode("utf-8")
    return urllib.request.Request(
        config.endpoint,
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )


def _classify_http_error(exc: urllib.error.HTTPError) -> HubStreamingError:
    if exc.code in (401, 403):
        return HubStreamingError("strata_auth_failed", "Strata authentication failed", http_status=exc.code)
    if exc.code in (408, 504):
        return HubStreamingError("strata_timeout", "Strata request timed out", http_status=exc.code)
    if exc.code in (404, 405, 409, 422):
        return HubStreamingError("strata_protocol_error", "Strata streaming endpoint rejected the request", http_status=exc.code)
    if 500 <= exc.code <= 599:
        return HubStreamingError("strata_upstream_error", "Strata returned an upstream error", http_status=exc.code)
    return HubStreamingError("strata_http_error", "Strata returned an unexpected HTTP status", http_status=exc.code)


def _open_response(
    request: urllib.request.Request,
    *,
    opener: Callable[..., object],
    timeout: float,
) -> object:
    try:
        return opener(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as exc:
        raise HubStreamingError("strata_connection_failed", "Strata connection failed") from exc


def iter_strata_deltas(response: object, *, chunk_size: int = 256, require_done: bool = True) -> Iterator[str]:
    decoder = SSEDecoder(require_done=require_done)
    try:
        while True:
            # HTTPResponse.readline() returns as soon as an SSE line is complete;
            # using read(256) can wait for the requested amount on some buffered
            # transports and unnecessarily delay the first event.
            readline = getattr(response, "readline", None)
            chunk = readline() if callable(readline) else response.read(chunk_size)
            if not chunk:
                break
            for delta in decoder.feed(chunk):
                yield delta
        for delta in decoder.finish():
            yield delta
    except StreamTimeout as exc:
        raise HubStreamingError("strata_timeout", "Strata stream timed out") from exc
    except StreamDisconnected as exc:
        raise HubStreamingError(
            "strata_connection_lost", "Strata stream disconnected"
        ) from exc
    except SSEParseError as exc:
        raise HubStreamingError("strata_invalid_sse", "Strata returned an invalid SSE event") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise HubStreamingError("strata_timeout", "Strata stream timed out") from exc
    except (ConnectionError, OSError) as exc:
        raise HubStreamingError("strata_connection_lost", "Strata stream disconnected") from exc


def stream_from_strata(
    instructions: str,
    user_text: str,
    *,
    config: HubStreamConfig = HubStreamConfig(),
    credential_reader: Callable[[], Optional[str]],
    opener: Callable[..., object] = urllib.request.urlopen,
) -> Iterator[str]:
    """Open the protected Strata stream without exposing credentials downstream."""
    api_key = credential_reader()
    if not api_key:
        raise HubStreamingError("strata_auth_unavailable", "Strata credential is unavailable")
    request = build_strata_request(config, instructions, user_text, api_key)
    response = _open_response(request, opener=opener, timeout=config.timeout)
    try:
        yield from iter_strata_deltas(response, chunk_size=config.chunk_size)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def collect_stream(iterator: Iterator[str]) -> HubStreamResult:
    deltas = tuple(iterator)
    return HubStreamResult(text="".join(deltas), deltas=deltas)


def choose_inference_path(
    enabled: bool,
    streaming_fn: Callable[[], object],
    legacy_fn: Callable[[], object],
) -> object:
    return streaming_fn() if enabled else legacy_fn()
