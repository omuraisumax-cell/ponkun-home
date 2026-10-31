import io
import json
import unittest
import urllib.error

from ponkun_hub.hub_streaming import (
    HubStreamConfig,
    HubStreamingError,
    build_strata_request,
    choose_inference_path,
    collect_stream,
    stream_from_strata,
)


class FakeResponse:
    def __init__(self, chunks):
        self.stream = io.BytesIO(b"".join(chunks))
        self.closed = False

    def read(self, size):
        return self.stream.read(size)

    def close(self):
        self.closed = True


def event(text):
    return ('data: ' + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + '\n\n').encode('utf-8')


class HubStreamingTests(unittest.TestCase):
    def test_request_contains_streaming_payload_without_key_in_body(self):
        request = build_strata_request(HubStreamConfig(), "system", "質問", "secret-in-memory")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "iq3_s")
        self.assertNotIn("secret-in-memory", request.data.decode("utf-8"))

    def test_mock_sse_is_forwarded_incrementally_and_collected(self):
        response = FakeResponse([event("こん"), event("にちは"), b'data: [DONE]\n\n'])
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), timeout, request.headers.get("Accept")))
            return response

        result = collect_stream(stream_from_strata(
            "system", "質問", credential_reader=lambda: "secret-in-memory", opener=opener,
        ))
        self.assertEqual(result.deltas, ("こん", "にちは"))
        self.assertEqual(result.text, "こんにちは")
        self.assertEqual(calls, [("POST", 90.0, "text/event-stream")])
        self.assertTrue(response.closed)

    def test_auth_error_is_classified_without_body_logging(self):
        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError("url", 401, "hidden", {}, None)

        with self.assertRaises(HubStreamingError) as ctx:
            list(stream_from_strata("system", "質問", credential_reader=lambda: "secret", opener=opener))
        self.assertEqual(ctx.exception.code, "strata_auth_failed")
        self.assertEqual(ctx.exception.http_status, 401)

    def test_missing_credential_does_not_call_opener(self):
        called = []

        def opener(*_args, **_kwargs):
            called.append(True)
            return FakeResponse([])

        with self.assertRaises(HubStreamingError) as ctx:
            list(stream_from_strata("system", "質問", credential_reader=lambda: None, opener=opener))
        self.assertEqual(ctx.exception.code, "strata_auth_unavailable")
        self.assertEqual(called, [])

    def test_invalid_sse_is_classified(self):
        response = FakeResponse([b'data: invalid-json\n\n'])
        with self.assertRaises(HubStreamingError) as ctx:
            list(stream_from_strata("system", "質問", credential_reader=lambda: "secret", opener=lambda *_a, **_k: response))
        self.assertEqual(ctx.exception.code, "strata_invalid_sse")

    def test_disconnect_is_classified(self):
        response = FakeResponse([event("途中")])
        with self.assertRaises(HubStreamingError) as ctx:
            list(stream_from_strata("system", "質問", credential_reader=lambda: "secret", opener=lambda *_a, **_k: response))
        self.assertEqual(ctx.exception.code, "strata_connection_lost")

    def test_timeout_is_classified(self):
        class TimeoutResponse:
            def read(self, _size):
                raise TimeoutError
            def close(self):
                pass

        with self.assertRaises(HubStreamingError) as ctx:
            list(stream_from_strata("system", "質問", credential_reader=lambda: "secret", opener=lambda *_a, **_k: TimeoutResponse()))
        self.assertEqual(ctx.exception.code, "strata_timeout")

    def test_streaming_off_keeps_legacy_path(self):
        called = []
        result = choose_inference_path(False, lambda: "stream", lambda: called.append("legacy") or "legacy")
        self.assertEqual(result, "legacy")
        self.assertEqual(called, ["legacy"])


if __name__ == "__main__":
    unittest.main()
