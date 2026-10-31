import threading
import time
import unittest

from ponkun_hub.streaming_inference import StreamDisconnected
from ponkun_hub.streaming_pipeline import (
    PipelineResult,
    run_sse_tts_pipeline,
    run_with_legacy_fallback,
)
from ponkun_hub.streaming_voice import SentenceBuffer


def sse_content(text):
    return 'data: {"choices":[{"delta":{"content":"' + text + '"}}]}\n\n'


class StreamingPipelineTests(unittest.TestCase):
    def test_sse_to_tts_preserves_complete_answer(self):
        played = []
        result = run_sse_tts_pipeline(
            [sse_content("これは"), sse_content("最初です。"), sse_content("次です。"), "data: [DONE]\n\n"],
            lambda text: text.encode(),
            lambda audio: played.append(audio.decode()),
            sentence_buffer=SentenceBuffer(min_chars=4),
        )
        self.assertEqual(result.text, "これは最初です。次です。")
        self.assertEqual(played, ["これは最初です。", "次です。"])

    def test_tts_starts_before_later_sse_chunk_arrives(self):
        tts_started = threading.Event()
        release_tts = threading.Event()
        observed = []

        def chunks():
            yield sse_content("先行文です。")
            self.assertTrue(tts_started.wait(0.5))
            observed.append("later_chunk_received")
            yield sse_content("後続文です。")
            yield "data: [DONE]\n\n"
            release_tts.set()

        def tts(text):
            tts_started.set()
            release_tts.wait(0.5)
            return text

        result = run_sse_tts_pipeline(chunks(), tts, lambda _: None, max_workers=2, sentence_buffer=SentenceBuffer(min_chars=4))
        self.assertEqual(observed, ["later_chunk_received"])
        self.assertEqual(result.metrics.tts_call_count, 2)

    def test_reverse_tts_completion_keeps_playback_order(self):
        completed = []
        played = []

        def tts(text):
            time.sleep(0.03 if text.startswith("先") else 0.005)
            completed.append(text)
            return text

        result = run_sse_tts_pipeline(
            [sse_content("先行文です。"), sse_content("後続文です。"), "data: [DONE]\n\n"],
            tts,
            played.append,
            max_workers=2,
            sentence_buffer=SentenceBuffer(min_chars=4),
        )
        self.assertEqual(completed, ["後続文です。", "先行文です。"])
        self.assertEqual(played, ["先行文です。", "後続文です。"])
        self.assertEqual(result.text, "先行文です。後続文です。")

    def test_short_sentences_keep_merge_policy(self):
        result = run_sse_tts_pipeline(
            [sse_content("短い。"), sse_content("次の文もあります。"), "data: [DONE]\n\n"],
            lambda text: text,
            lambda _: None,
            sentence_buffer=SentenceBuffer(min_chars=10),
        )
        self.assertEqual(result.text, "短い。次の文もあります。")
        self.assertEqual(result.metrics.emitted_sentences, 1)

    def test_disconnect_is_reported_after_partial_stream(self):
        with self.assertRaises(StreamDisconnected):
            run_sse_tts_pipeline(
                [sse_content("途中まで。")],
                lambda text: text,
                lambda _: None,
                sentence_buffer=SentenceBuffer(min_chars=4),
            )

    def test_tts_failure_calls_fallback(self):
        fallback_calls = []
        played = []

        def tts(text):
            if text.startswith("失敗"):
                raise RuntimeError("mock failure")
            return text

        def fallback(text):
            fallback_calls.append(text)
            return "代替:" + text

        result = run_sse_tts_pipeline(
            [sse_content("正常文です。"), sse_content("失敗文です。"), "data: [DONE]\n\n"],
            tts,
            played.append,
            fallback_tts_fn=fallback,
            sentence_buffer=SentenceBuffer(min_chars=4),
        )
        self.assertEqual(fallback_calls, ["失敗文です。"])
        self.assertEqual(played, ["正常文です。", "代替:失敗文です。"])
        self.assertEqual(result.metrics.failed_sentences, ())

    def test_metrics_include_first_audio_total_time_and_calls(self):
        result = run_sse_tts_pipeline(
            [sse_content("計測文です。"), "data: [DONE]\n\n"],
            lambda text: (time.sleep(0.005), text)[1],
            lambda _: None,
            sentence_buffer=SentenceBuffer(min_chars=4),
        )
        self.assertIsNotNone(result.metrics.time_to_first_audio_ms)
        self.assertGreaterEqual(result.metrics.total_turn_ms, result.metrics.time_to_first_audio_ms)
        self.assertEqual(result.metrics.tts_call_count, 1)

    def test_streaming_off_uses_legacy_function(self):
        called = []
        streaming = PipelineResult("stream", None)
        result = run_with_legacy_fallback(False, lambda: streaming, lambda: called.append("legacy") or "legacy-result")
        self.assertEqual(result, "legacy-result")
        self.assertEqual(called, ["legacy"])


if __name__ == "__main__":
    unittest.main()
