import importlib.util
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("tts_proxy.py")


def load_without_creating_app():
    source = MODULE_PATH.read_text(encoding="utf-8")
    source = source.replace("app = create_app()\n", "app = None\n")
    namespace = {"__name__": "secure_tts_proxy_test", "__file__": str(MODULE_PATH)}
    exec(compile(source, str(MODULE_PATH), "exec"), namespace)
    return namespace


class SecureProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_without_creating_app()

    def test_missing_secret_fails_closed(self):
        with self.assertRaises(self.module["ProxyConfigurationError"]):
            self.module["read_api_key_file"]("missing-secret-file")

    def test_empty_secret_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("   \n", encoding="utf-8")
            with self.assertRaises(self.module["ProxyConfigurationError"]):
                self.module["read_api_key_file"](path)

    def test_bom_or_newline_secret_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            for raw in (b"x" * 32 + b"\n", b"\xef\xbb\xbf" + b"x" * 32):
                path.write_bytes(raw)
                with self.assertRaises(self.module["ProxyConfigurationError"]):
                    self.module["read_api_key_file"](path)

    def test_auth_failure_is_sanitized(self):
        self.assertEqual(
            self.module["classify_upstream_status"](401),
            (502, "upstream_auth_failed"),
        )
        self.assertNotIn("response", self.module["classify_upstream_status"](403)[1])

    def test_upstream_statuses_are_safely_classified(self):
        expected = {
            400: (502, "upstream_bad_request"),
            401: (502, "upstream_auth_failed"),
            403: (502, "upstream_auth_failed"),
            422: (502, "upstream_validation_failed"),
            429: (503, "upstream_rate_limited"),
            500: (502, "upstream_unavailable"),
        }
        for status, classification in expected.items():
            with self.subTest(status=status):
                self.assertEqual(
                    self.module["classify_upstream_status"](status),
                    classification,
                )

    def test_cloud_request_uses_model_header_not_json_field(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"model": body.model', source)
        payload_block = source.split("payload = {", 1)[1].split("}", 1)[0]
        self.assertNotIn('"model"', payload_block)
        self.assertIn('"Authorization": "Bearer " + api_key', source)
        self.assertIn('"Content-Type": "application/json"', source)

    def test_source_has_no_literal_bearer_credential(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("api-key", source.casefold())
        self.assertIn('"Bearer " + api_key', source)

    def test_openai_compatible_fields_are_required_nonempty_strings(self):
        app = self.module["create_app"](
            secret_reader=lambda _path: "x" * 20,
            post=lambda *args, **kwargs: None,
        )
        schema = app.openapi()["components"]["schemas"]["SpeechRequest"]
        self.assertEqual(
            set(schema["required"]),
            {"model", "input", "voice", "response_format"},
        )
        for field in schema["required"]:
            self.assertEqual(schema["properties"][field]["type"], "string")
            self.assertEqual(schema["properties"][field]["minLength"], 1)

    def test_local_validation_failure_is_sanitized_and_identified(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"X-Ponkun-Validation-Origin": "local"', source)
        self.assertIn('"detail": "local_request_validation_failed"', source)
        self.assertNotIn("_exc.errors()", source)

    def test_valid_input_reaches_upstream_without_local_422(self):
        from fastapi.testclient import TestClient

        upstream = SimpleNamespace(
            status_code=200,
            content=b"mock-audio",
            headers={"content-type": "audio/mpeg"},
        )
        app = self.module["create_app"](
            secret_reader=lambda _path: "x" * 20,
            post=lambda *args, **kwargs: upstream,
        )
        response = TestClient(app).post(
            "/v1/audio/speech",
            json={
                "model": "s2-pro",
                "input": "safe mock input",
                "voice": "mock-reference",
                "response_format": "mp3",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(
            response.headers.get("X-Ponkun-Validation-Origin"),
            "local",
        )


if __name__ == "__main__":
    unittest.main()
