"""Opt-in PC Voice bridge for the independent Hub streaming adapter."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterable, Iterator
from typing import Callable, Mapping, Optional

from ponkun_hub.streaming_inference import SSEDecoder
from ponkun_hub.streaming_pipeline import run_sse_tts_pipeline
from ponkun_hub.streaming_voice import SentenceBuffer


def streaming_voice_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    values = os.environ if env is None else env
    return values.get("PONKUN_STREAMING_VOICE", "0").strip() == "1"


def _delta_frames(deltas: Iterable[str]) -> Iterator[bytes]:
    for delta in deltas:
        payload = {"choices": [{"delta": {"content": delta}}]}
        yield ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
    yield b"data: [DONE]\n\n"


def stream_hub_deltas(
    user_text: str,
    *,
    instructions: str = "",
    hub_url: Optional[str] = None,
    client_token: Optional[str] = None,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> Iterator[str]:
    """Read Hub SSE deltas without ever handling a Strata credential."""
    endpoint = (hub_url or os.environ.get("PONKUN_HUB_URL", "http://127.0.0.1:8788")).rstrip("/")
    payload = json.dumps({
        "instructions": instructions,
        "user_text": user_text,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    token = client_token if client_token is not None else os.environ.get("PONKUN_HUB_CLIENT_TOKEN", "")
    if token:
        headers["X-Ponkun-Client-Token"] = token
    request = urllib.request.Request(
        endpoint + "/api/inference/stream",
        data=payload,
        headers=headers,
        method="POST",
    )
    response = opener(request, timeout=90)
    decoder = SSEDecoder(require_done=True)
    try:
        while True:
            readline = getattr(response, "readline", None)
            chunk = readline() if callable(readline) else response.read(256)
            if not chunk:
                break
            yield from decoder.feed(chunk)
        yield from decoder.finish()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def answer_with_optional_streaming(
    *,
    enabled: bool,
    deltas: Iterable[str],
    tts_generate: Callable[[str], object],
    play_audio: Callable[[object], None],
    fallback_tts: Optional[Callable[[str], object]] = None,
    legacy_answer: Callable[[], object],
    min_sentence_chars: int = 12,
) -> object:
    """Use streaming only when explicitly enabled; otherwise call legacy path."""
    if not enabled:
        return legacy_answer()
    return run_sse_tts_pipeline(
        _delta_frames(deltas),
        tts_fn=tts_generate,
        play_fn=play_audio,
        fallback_tts_fn=fallback_tts,
        sentence_buffer=SentenceBuffer(min_chars=min_sentence_chars),
    )
