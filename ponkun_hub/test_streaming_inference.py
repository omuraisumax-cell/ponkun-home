import unittest

from ponkun_hub.streaming_inference import (
    SSEParseError,
    StreamDisconnected,
    StreamTimeout,
    build_stream_payload,
    consume_sse,
    streaming_opt_in,
)


class StreamingInferenceTests(unittest.TestCase):
    def test_split_events_and_multiline_data(self):
        result = consume_sse([
            'data: {"choices":[{"delta":{"content":"こん"}}]}\r'.encode("utf-8"),
            b'\n\n',
            'data: {"choices":[{"delta":{"content":"にちは"}}]}\n\n',
            'data: [DONE]\n\n',
        ])
        self.assertEqual(result.deltas, ("こん", "にちは"))
        self.assertEqual(result.text, "こんにちは")

    def test_utf8_character_split_across_byte_chunks(self):
        payload = 'data: {"choices":[{"delta":{"content":"日"}}]}\n\ndata: [DONE]\n\n'.encode()
        result = consume_sse([payload[:12], payload[12:]])
        self.assertEqual(result.text, "日")

    def test_multiple_data_lines_are_joined_before_json_parse(self):
        result = consume_sse([
            'data: {"choices":[{"delta":{"content":"A"}}',
            ']}' + '\n\n',
            'data: [DONE]\n\n',
        ])
        self.assertEqual(result.text, "A")

    def test_done_marker_and_empty_metadata_events(self):
        result = consume_sse([': keepalive\n\n', 'data: [DONE]\n\n'])
        self.assertEqual(result.text, "")

    def test_invalid_json_is_classified(self):
        with self.assertRaises(SSEParseError):
            consume_sse(['data: not-json\n\n'])

    def test_disconnect_before_done_is_classified(self):
        with self.assertRaises(StreamDisconnected):
            consume_sse(['data: {"choices":[{"delta":{"content":"x"}}]}\n\n'])

    def test_timeout_is_classified(self):
        def chunks():
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            raise TimeoutError

        with self.assertRaises(StreamTimeout):
            consume_sse(chunks())

    def test_payload_is_streaming_and_does_not_include_credentials(self):
        payload = build_stream_payload("iq3_s", "system", "質問")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "iq3_s")
        self.assertNotIn("Authorization", payload)

    def test_opt_in_is_off_by_default(self):
        self.assertFalse(streaming_opt_in({}))
        self.assertFalse(streaming_opt_in({"PONKUN_STREAMING_VOICE": "0"}))
        self.assertTrue(streaming_opt_in({"PONKUN_STREAMING_VOICE": "1"}))


if __name__ == "__main__":
    unittest.main()
