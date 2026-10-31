"""Opt-in, dependency-free sentence-to-TTS streaming primitives.

This module is intentionally independent from Ponkun's live PC, Hub, and TTS
paths.  It accepts already-produced text chunks and injected TTS/playback
functions, so tests never call Qwen, Fish Audio, a microphone, or a speaker.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional


def streaming_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether the experimental path is explicitly enabled."""
    env = environ if environ is not None else os.environ
    return env.get("PONKUN_STREAMING_VOICE", "0").strip() == "1"


@dataclass(frozen=True)
class StreamMetrics:
    time_to_first_token_ms: Optional[int]
    time_to_first_sentence_ms: Optional[int]
    time_to_first_audio_ms: Optional[int]
    tts_generation_ms: int
    playback_completion_ms: int
    total_turn_ms: int
    tts_call_count: int
    emitted_sentences: int
    failed_sentences: tuple[int, ...] = ()


@dataclass
class SentenceBuffer:
    """Incrementally split Japanese text while coalescing tiny sentences."""

    min_chars: int = 12
    max_chars: int = 320
    timeout_seconds: float = 1.5
    _buffer: str = field(default="", init=False)
    _started_at: Optional[float] = field(default=None, init=False)

    _terminators = frozenset("。！？!?\n")
    _closers = frozenset("」』）)]】〉》’\"'")

    def _boundary(self, text: str) -> Optional[int]:
        for index, char in enumerate(text):
            if char not in self._terminators:
                continue
            end = index + 1
            while end < len(text) and text[end] in self._closers:
                end += 1
            return end
        return None

    def _take_ready(self, final: bool = False) -> list[str]:
        result: list[str] = []
        while self._buffer:
            boundary = self._boundary(self._buffer)
            if boundary is None:
                if len(self._buffer) > self.max_chars:
                    cut = self._buffer.rfind(" ", 0, self.max_chars + 1)
                    cut = cut if cut >= self.min_chars else self.max_chars
                    result.append(self._buffer[:cut].strip())
                    self._buffer = self._buffer[cut:].lstrip()
                    continue
                break
            candidate = self._buffer[:boundary].strip()
            if len(candidate) < self.min_chars and not final:
                next_boundary = self._boundary(self._buffer[boundary:])
                if next_boundary is None:
                    break
                boundary += next_boundary
                candidate = self._buffer[:boundary].strip()
                if len(candidate) < self.min_chars:
                    break
            result.append(candidate)
            self._buffer = self._buffer[boundary:].lstrip()
        if final and self._buffer.strip():
            result.append(self._buffer.strip())
            self._buffer = ""
        if not self._buffer:
            self._started_at = None
        return [item for item in result if item]

    def feed(self, chunk: str, now: Optional[float] = None) -> list[str]:
        if not chunk:
            return []
        if self._started_at is None:
            self._started_at = time.monotonic() if now is None else now
        self._buffer += chunk
        return self._take_ready()

    def flush_if_due(self, now: Optional[float] = None) -> list[str]:
        if not self._buffer or self._started_at is None:
            return []
        current = time.monotonic() if now is None else now
        if current - self._started_at < self.timeout_seconds:
            return []
        if len(self._buffer.strip()) < self.min_chars:
            return []
        return self._take_ready(final=True)

    def finish(self) -> list[str]:
        return self._take_ready(final=True)


class StreamingVoiceError(RuntimeError):
    """Raised only when a configured fallback cannot recover a TTS failure."""


class OrderedTTSRunner:
    """Generate TTS concurrently but invoke playback strictly by sentence index."""

    def __init__(
        self,
        tts_fn: Callable[[str], object],
        play_fn: Callable[[object], None],
        *,
        max_workers: int = 1,
        fallback_tts_fn: Optional[Callable[[str], object]] = None,
        clock: Callable[[], float] = time.perf_counter,
        on_tts_complete: Optional[Callable[[int, bool], None]] = None,
        on_playback_start: Optional[Callable[[int], None]] = None,
        on_playback_complete: Optional[Callable[[int], None]] = None,
    ) -> None:
        if max_workers < 1 or max_workers > 2:
            raise ValueError("max_workers must be 1 or 2")
        self._tts_fn = tts_fn
        self._play_fn = play_fn
        self._fallback_tts_fn = fallback_tts_fn
        self._clock = clock
        self._on_tts_complete = on_tts_complete
        self._on_playback_start = on_playback_start
        self._on_playback_complete = on_playback_complete
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._condition = threading.Condition()
        self._futures: dict[int, Future] = {}
        self._next_index = 0
        self._closed = False
        self._worker_error: Optional[BaseException] = None
        self._failed: list[int] = []
        self._playback_started_at: Optional[float] = None
        self._playback_finished_at: Optional[float] = None
        self._tts_started_at: Optional[float] = None
        self._tts_finished_at: Optional[float] = None
        self._tts_call_count = 0
        self._fallback_tts_call_count = 0
        self._played_count = 0
        self._worker = threading.Thread(target=self._play_in_order, daemon=True)
        self._worker.start()

    def _generate(self, index: int, text: str) -> object:
        with self._condition:
            self._tts_call_count += 1
            if self._tts_started_at is None:
                self._tts_started_at = self._clock()
        fallback_used = False
        try:
            try:
                audio = self._tts_fn(text)
            except Exception:
                if self._fallback_tts_fn is None:
                    raise
                fallback_used = True
                with self._condition:
                    self._fallback_tts_call_count += 1
                audio = self._fallback_tts_fn(text)
            if self._on_tts_complete is not None:
                self._on_tts_complete(index, fallback_used)
            return audio
        finally:
            with self._condition:
                self._tts_finished_at = self._clock()

    def submit(self, index: int, text: str) -> None:
        if index < 0 or not text.strip():
            raise ValueError("index must be non-negative and text must be non-empty")
        with self._condition:
            if self._closed or index in self._futures:
                raise RuntimeError("TTS runner is closed or index already submitted")
            self._futures[index] = self._executor.submit(self._generate, index, text)
            self._condition.notify_all()

    def _play_in_order(self) -> None:
        while True:
            with self._condition:
                while self._next_index not in self._futures and not self._closed:
                    self._condition.wait()
                if self._next_index not in self._futures and self._closed:
                    self._playback_finished_at = self._clock()
                    return
                index = self._next_index
                future = self._futures[index]
            try:
                audio = future.result()
                if self._playback_started_at is None:
                    self._playback_started_at = self._clock()
                if self._on_playback_start is not None:
                    self._on_playback_start(index)
                self._play_fn(audio)
                if self._on_playback_complete is not None:
                    self._on_playback_complete(index)
                self._played_count += 1
            except Exception as exc:
                self._failed.append(index)
                if self._worker_error is None:
                    self._worker_error = exc
            finally:
                with self._condition:
                    self._futures.pop(index, None)
                    self._next_index += 1
                    self._condition.notify_all()

    def close(self) -> tuple[int, ...]:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._worker.join()
        self._executor.shutdown(wait=True)
        return tuple(self._failed)

    @property
    def first_audio_time(self) -> Optional[float]:
        return self._playback_started_at

    @property
    def playback_finished_time(self) -> Optional[float]:
        return self._playback_finished_at

    @property
    def tts_generation_start(self) -> Optional[float]:
        return self._tts_started_at

    @property
    def tts_generation_end(self) -> Optional[float]:
        return self._tts_finished_at

    @property
    def tts_call_count(self) -> int:
        return self._tts_call_count

    @property
    def fallback_tts_call_count(self) -> int:
        return self._fallback_tts_call_count

    @property
    def played_count(self) -> int:
        return self._played_count


def run_streaming_turn(
    chunks: Iterable[str],
    tts_fn: Callable[[str], object],
    play_fn: Callable[[object], None],
    *,
    max_workers: int = 1,
    fallback_tts_fn: Optional[Callable[[str], object]] = None,
    buffer: Optional[SentenceBuffer] = None,
    inter_chunk_delay: float = 0.0,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[str, StreamMetrics]:
    """Run a synthetic LLM chunk stream without making any network/audio call."""
    started = clock()
    first_token_at: Optional[float] = None
    first_sentence_at: Optional[float] = None
    all_text: list[str] = []
    sentence_buffer = buffer or SentenceBuffer()
    runner = OrderedTTSRunner(
        tts_fn, play_fn, max_workers=max_workers,
        fallback_tts_fn=fallback_tts_fn, clock=clock,
    )
    next_index = 0
    try:
        for chunk in chunks:
            if first_token_at is None:
                first_token_at = clock()
            all_text.append(chunk)
            sentences = sentence_buffer.feed(chunk)
            if sentences and first_sentence_at is None:
                first_sentence_at = clock()
            for sentence in sentences:
                runner.submit(next_index, sentence)
                next_index += 1
            if inter_chunk_delay:
                time.sleep(inter_chunk_delay)
        sentences = sentence_buffer.finish()
        if sentences and first_sentence_at is None:
            first_sentence_at = clock()
        for sentence in sentences:
            runner.submit(next_index, sentence)
            next_index += 1
    finally:
        failed = runner.close()
    ended = clock()
    first_audio = runner.first_audio_time
    tts_start = runner.tts_generation_start
    tts_end = runner.tts_generation_end
    playback_end = runner.playback_finished_time or ended
    metrics = StreamMetrics(
        time_to_first_token_ms=None if first_token_at is None else round((first_token_at - started) * 1000),
        time_to_first_sentence_ms=None if first_sentence_at is None else round((first_sentence_at - started) * 1000),
        time_to_first_audio_ms=None if first_audio is None else round((first_audio - started) * 1000),
        tts_generation_ms=0 if tts_start is None or tts_end is None else round((tts_end - tts_start) * 1000),
        playback_completion_ms=round((playback_end - started) * 1000),
        total_turn_ms=round((ended - started) * 1000),
        tts_call_count=runner.tts_call_count,
        emitted_sentences=next_index,
        failed_sentences=failed,
    )
    return "".join(all_text), metrics

