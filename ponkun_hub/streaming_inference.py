"""Opt-in SSE client primitives for a future Hub -> PC Voice stream.

This module is deliberately not imported by the existing inference route.  It
parses OpenAI-compatible SSE responses from an injected response object and
keeps the complete answer while yielding text deltas.  Tests use fake
responses, so no Strata, Hub, or external service is contacted.
"""

from __future__ import annotations

import json
import codecs
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional


class StreamingInferenceError(RuntimeError):
    """Base error for the independent streaming path."""


class SSEParseError(StreamingInferenceError):
    """Raised when an SSE event contains invalid JSON or an invalid delta."""


class StreamDisconnected(StreamingInferenceError):
    """Raised when the response ends before the explicit [DONE] marker."""


class StreamTimeout(StreamingInferenceError):
    """Reserved for the transport adapter's timeout mapping."""


@dataclass
class SSEEvent:
    data: list[str] = field(default_factory=list)

    def payload(self) -> str:
        return "\n".join(self.data)


class SSEDecoder:
    """Decode arbitrarily split SSE bytes/text into content deltas."""

    def __init__(self, *, require_done: bool = True) -> None:
        self.require_done = require_done
        self._line_buffer = ""
        self._event = SSEEvent()
        self.done = False
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes | str) -> list[str]:
        if self.done:
            return []
        text = self._utf8_decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        self._line_buffer += text
        output: list[str] = []
        while True:
            line_end = self._find_line_end(self._line_buffer)
            if line_end is None:
                break
            line, consumed = line_end
            self._line_buffer = self._line_buffer[consumed:]
            output.extend(self._consume_line(line))
            if self.done:
                break
        return output

    @staticmethod
    def _find_line_end(value: str) -> Optional[tuple[str, int]]:
        for index, char in enumerate(value):
            if char == "\n":
                return value[:index].rstrip("\r"), index + 1
            if char == "\r":
                consumed = index + 1
                if consumed < len(value) and value[consumed] == "\n":
                    consumed += 1
                return value[:index], consumed
        return None

    def _consume_line(self, line: str) -> list[str]:
        if line == "":
            return self._finish_event()
        if line.startswith(":"):
            return []
        field, separator, value = line.partition(":")
        if not separator:
            return []
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._event.data.append(value)
        return []

    def _finish_event(self) -> list[str]:
        if not self._event.data:
            return []
        payload = self._event.payload()
        self._event = SSEEvent()
        if payload == "[DONE]":
            self.done = True
            return []
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SSEParseError("SSE event JSON is invalid") from exc
        delta = extract_content_delta(data)
        return [] if delta is None else [delta]

    def finish(self) -> list[str]:
        output: list[str] = []
        self._line_buffer += self._utf8_decoder.decode(b"", final=True)
        if self._line_buffer:
            output.extend(self._consume_line(self._line_buffer.rstrip("\r")))
            self._line_buffer = ""
        output.extend(self._finish_event())
        if self.require_done and not self.done:
            raise StreamDisconnected("SSE stream ended before [DONE]")
        return output


def extract_content_delta(data: object) -> Optional[str]:
    """Extract a chat-completions delta without accepting arbitrary fields."""
    if not isinstance(data, dict):
        raise SSEParseError("SSE payload is not an object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        raise SSEParseError("SSE choice is invalid")
    delta = choice.get("delta")
    if delta is None:
        return None
    if not isinstance(delta, dict):
        raise SSEParseError("SSE delta is invalid")
    content = delta.get("content")
    if content is None:
        return None
    if not isinstance(content, str):
        raise SSEParseError("SSE content delta is invalid")
    return content


@dataclass(frozen=True)
class StreamResult:
    text: str
    deltas: tuple[str, ...]


def consume_sse(chunks: Iterable[bytes | str], *, require_done: bool = True) -> StreamResult:
    """Consume a fake or real response body and retain the complete answer."""
    decoder = SSEDecoder(require_done=require_done)
    deltas: list[str] = []
    try:
        for chunk in chunks:
            deltas.extend(decoder.feed(chunk))
    except TimeoutError as exc:
        raise StreamTimeout("SSE stream timed out") from exc
    deltas.extend(decoder.finish())
    return StreamResult(text="".join(deltas), deltas=tuple(deltas))


def build_stream_payload(model: str, instructions: str, user_text: str) -> dict:
    """Build a request body for a future OpenAI-compatible streaming adapter."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_text},
        ],
        "stream": True,
    }


def streaming_opt_in(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = environ if environ is not None else {}
    return env.get("PONKUN_STREAMING_VOICE", "0").strip() == "1"
