import time
import unittest

from ponkun_hub.streaming_voice import (
    SentenceBuffer,
    run_streaming_turn,
    streaming_enabled,
)


class SentenceBufferTests(unittest.TestCase):
    def test_japanese_boundaries_across_chunks(self):
        buffer = SentenceBuffer(min_chars=4)
        self.assertEqual(buffer.feed("これは最初"), [])
        self.assertEqual(buffer.feed("の文です。次"), ["これは最初の文です。"])
        self.assertEqual(buffer.finish(), ["次"])

    def test_short_sentence_is_joined_until_finish(self):
        buffer = SentenceBuffer(min_chars=10)
        self.assertEqual(buffer.feed("短い。"), [])
        self.assertEqual(buffer.feed("次の文もあります。"), ["短い。次の文もあります。"])

    def test_buffer_has_a_hard_upper_bound(self):
        buffer = SentenceBuffer(min_chars=4, max_chars=10)
        pieces = buffer.feed("abcdefghijk")
        self.assertEqual(pieces, ["abcdefghij"])
        self.assertEqual(buffer.finish(), ["k"])

    def test_timeout_flushes_long_incomplete_sentence(self):
        buffer = SentenceBuffer(min_chars=4, timeout_seconds=1.0)
        self.assertEqual(buffer.feed("まだ続く文章", now=10.0), [])
        self.assertEqual(buffer.flush_if_due(now=10.5), [])
        self.assertEqual(buffer.flush_if_due(now=11.1), ["まだ続く文章"])

    def test_streaming_is_opt_in(self):
        self.assertFalse(streaming_enabled({}))
        self.assertFalse(streaming_enabled({"PONKUN_STREAMING_VOICE": "0"}))
        self.assertTrue(streaming_enabled({"PONKUN_STREAMING_VOICE": "1"}))


class OrderedPipelineTests(unittest.TestCase):
    def test_tts_completion_order_does_not_change_playback_order(self):
        generated = []
        completed = []
        played = []

        def tts(text):
            generated.append(text)
            time.sleep(0.03 if text.startswith("一") else 0.005)
            completed.append(text)
            return text.encode()

        answer, metrics = run_streaming_turn(
            ["一番目の文です。", "二番目の文です。"],
            tts,
            lambda audio: played.append(audio.decode()),
            max_workers=2,
            buffer=SentenceBuffer(min_chars=4),
        )
        self.assertEqual(answer, "一番目の文です。二番目の文です。")
        self.assertEqual(completed, ["二番目の文です。", "一番目の文です。"])
        self.assertEqual(played, ["一番目の文です。", "二番目の文です。"])
        self.assertEqual(metrics.tts_call_count, 2)
        self.assertEqual(metrics.failed_sentences, ())

    def test_tts_failure_uses_fallback_without_network(self):
        played = []
        fallback_calls = []

        def tts(text):
            if text.startswith("失敗"):
                raise RuntimeError("mock TTS failure")
            return text
        def fallback(text):
            fallback_calls.append(text)
            return "fallback:" + text

        answer, metrics = run_streaming_turn(
            ["正常な文です。失敗する文です。"],
            tts,
            played.append,
            fallback_tts_fn=fallback,
            buffer=SentenceBuffer(min_chars=4),
        )
        self.assertEqual(answer, "正常な文です。失敗する文です。")
        self.assertEqual(played, ["正常な文です。", "fallback:失敗する文です。"])
        self.assertEqual(fallback_calls, ["失敗する文です。"])
        self.assertEqual(metrics.failed_sentences, ())

    def test_metrics_include_first_audio_and_completion(self):
        _, metrics = run_streaming_turn(
            ["一文の回答です。"],
            lambda text: (time.sleep(0.01), text)[1],
            lambda audio: None,
        )
        self.assertIsNotNone(metrics.time_to_first_audio_ms)
        self.assertGreaterEqual(metrics.playback_completion_ms, metrics.time_to_first_audio_ms)
        self.assertEqual(metrics.tts_call_count, 1)


if __name__ == "__main__":
    unittest.main()
