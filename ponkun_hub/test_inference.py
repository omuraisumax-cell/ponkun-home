import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ponkun_inference as inference


class _Handler(BaseHTTPRequestHandler):
    received_auth = None
    response_status = 200

    def do_POST(self):
        if self.__class__.response_status != 200:
            self.send_response(self.__class__.response_status)
            self.end_headers()
            return
        self.__class__.received_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.received_client_token = self.headers.get("X-Ponkun-Client-Token")
        if "user_text" in payload:
            body = json.dumps({"text": "mock hub answer"}).encode()
        else:
            body = json.dumps({"choices": [{"message": {"content": "mock strata answer"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class InferenceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old = (inference.BACKEND, inference.STRATA_URL, inference.STRATA_API_KEY,
                    inference.HUB_URL, inference.HUB_CLIENT_TOKEN)
        inference.BACKEND = "strata"
        inference.STRATA_URL = f"http://127.0.0.1:{self.server.server_port}"
        inference.STRATA_API_KEY = "test-only-key"
        inference.HUB_URL = f"http://127.0.0.1:{self.server.server_port}"
        inference.HUB_CLIENT_TOKEN = "test-hub-token"
        _Handler.response_status = 200

    def tearDown(self):
        (inference.BACKEND, inference.STRATA_URL, inference.STRATA_API_KEY,
         inference.HUB_URL, inference.HUB_CLIENT_TOKEN) = self.old
        self.server.shutdown()
        self.server.server_close()

    def test_openai_compatible_mock(self):
        self.assertTrue(inference.is_enabled())
        self.assertEqual(inference.chat("qwen", "system", "hello"), "mock strata answer")
        self.assertEqual(_Handler.received_auth, "Bearer test-only-key")

    def test_disabled_without_endpoint(self):
        inference.STRATA_URL = ""
        self.assertFalse(inference.is_enabled())
        with self.assertRaises(inference.InferenceUnavailable):
            inference.chat("qwen", "system", "hello")

    def test_non_latin1_strata_key_is_rejected_before_network(self):
        network_calls = []
        original_urlopen = inference.urllib.request.urlopen
        inference.STRATA_API_KEY = "ab日本語-key"
        inference.urllib.request.urlopen = lambda *args, **kwargs: network_calls.append(True)
        try:
            with self.assertRaises(inference.InferenceUnavailable) as ctx:
                inference.chat("qwen", "system", "hello")
        finally:
            inference.urllib.request.urlopen = original_urlopen
        self.assertEqual(network_calls, [])
        self.assertIn("unsupported", str(ctx.exception))
        self.assertNotIn("日本語", str(ctx.exception))
        self.assertNotIn("ab", str(ctx.exception))

    def test_non_latin1_hub_token_is_rejected_before_network(self):
        network_calls = []
        original_urlopen = inference.urllib.request.urlopen
        inference.HUB_CLIENT_TOKEN = "token-日本語"
        inference.urllib.request.urlopen = lambda *args, **kwargs: network_calls.append(True)
        try:
            with self.assertRaises(inference.InferenceUnavailable) as ctx:
                inference.hub_chat("qwen", "system", "hello")
        finally:
            inference.urllib.request.urlopen = original_urlopen
        self.assertEqual(network_calls, [])
        self.assertIn("client token", str(ctx.exception))
        self.assertNotIn("日本語", str(ctx.exception))

    def test_hub_client_uses_separate_client_token(self):
        self.assertEqual(inference.hub_chat("qwen", "system", "hello"), "mock hub answer")
        self.assertIsNone(_Handler.received_auth)
        self.assertEqual(_Handler.received_client_token, "test-hub-token")

    def test_hub_http_status_is_classified_without_response_body(self):
        _Handler.response_status = 502
        with self.assertRaises(inference.InferenceUnavailable) as ctx:
            inference.hub_chat("qwen", "system", "hello")
        self.assertEqual(ctx.exception.code, "hub_http_502")
        self.assertEqual(ctx.exception.http_status, 502)
        self.assertEqual(ctx.exception.stage, "hub.http")
        self.assertNotIn("test-only-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
