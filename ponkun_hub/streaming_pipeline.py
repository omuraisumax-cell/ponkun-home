"""Mockable SSE-to-sentence-TTS integration for the opt-in voice prototype."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ponkun_hub.streaming_inference import SSEDecoder
from ponkun_hub.streaming_voice import (
    SentenceBuffer,
    StreamMetrics,
    OrderedTTSRunner,
)


@dataclass(frozen=True)
class PipelineResult:
    text: str
    metrics: StreamMetrics


def run_sse_tts_pipeline(
    chunks: Iterable[bytes | str],
    tts_fn: Callable[[str], object],
    play_fn: Callable[[object], None],
    *,
    max_workers: int = 1,
    fallback_tts_fn: Optional[Callable[[str], object]] = None,
    sentence_buffer: Optional[SentenceBuffer] = None,
    require_done: bool = True,
    clock: Callable[[], float] = time.perf_counter,
) -> PipelineResult:
    """Feed SSE deltas into sentence TTS while retaining the full answer."""
    started = clock()
    first_token_at = None
    first_sentence_at = None
    all_text: list[str] = []
    decoder = SSEDecoder(require_done=require_done)
    buffer = sentence_buffer or SentenceBuffer()
    runner = OrderedTTSRunner(
        tts_fn,
        play_fn,
        max_workers=max_workers,
        fallback_tts_fn=fallback_tts_fn,
        clock=clock,
    )
    next_index = 0

    def submit_deltas(deltas: list[str]) -> None:
        nonlocal first_token_at, first_sentence_at, next_index
        for delta in deltas:
            if first_token_at is None:
                first_token_at = clock()
            all_text.append(delta)
            sentences = buffer.feed(delta)
            if sentences and first_sentence_at is None:
                first_sentence_at = clock()
            for sentence in sentences:
                runner.submit(next_index, sentence)
                next_index += 1

    try:
        for chunk in chunks:
            submit_deltas(decoder.feed(chunk))
        submit_deltas(decoder.finish())
        sentences = buffer.finish()
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
    return PipelineResult(text="".join(all_text), metrics=metrics)


def run_with_legacy_fallback(
    enabled: bool,
    streaming_fn: Callable[[], PipelineResult],
    legacy_fn: Callable[[], object],
) -> object:
    """Keep the current voice path selected while streaming is disabled."""
    return streaming_fn() if enabled else legacy_fn()
