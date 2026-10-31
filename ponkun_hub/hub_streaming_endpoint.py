"""Unregistered HTTP adapter for the Hub streaming endpoint.

The current Hub uses a small stdlib HTTP handler rather than FastAPI.  This
module keeps route policy separate so it can be registered only after an
explicit deployment decision.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from typing import Callable, Mapping, Optional

from ponkun_hub.hub_streaming import HubStreamConfig, stream_from_strata


def stream_event_body(
    data: Mapping[str, object],
    *,
    client_authorized: bool,
    credential_reader: Callable[[], Optional[str]],
    config: HubStreamConfig = HubStreamConfig(),
    opener: Optional[Callable[..., object]] = None,
) -> Iterator[bytes]:
    """Yield an OpenAI-compatible SSE body; never yields credentials."""
    if not client_authorized:
        raise PermissionError("Hub client authentication failed")
    user_text = str(data.get("user_text", ""))
    if not user_text.strip():
        raise ValueError("user_text is required")
    instructions = str(data.get("instructions", ""))
    deltas = stream_from_strata(
        instructions,
        user_text,
        config=config,
        credential_reader=credential_reader,
        opener=urllib.request.urlopen if opener is None else opener,
    )
    for delta in deltas:
        payload = {"choices": [{"delta": {"content": delta}}]}
        yield ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
    yield b"data: [DONE]\n\n"
