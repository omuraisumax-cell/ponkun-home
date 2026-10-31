import io
import json
import unittest
import urllib.error

from ponkun_hub.strata_auth_diagnostic import run_once


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.auth_header = None

    def open(self, request, timeout):
        self.calls += 1
        self.auth_header = request.get_header("Authorization")
        if self.error:
            raise self.error
        return self.response


class StrataAuthDiagnosticTests(unittest.TestCase):
    def test_success_and_model_presence_without_secret_output(self):
        opener = FakeOpener(FakeResponse(json.dumps({"data": [{"id": "iq3_s"}, {"id": "other"}]}).encode()))
        result = run_once(key_reader=lambda: "unit-test-key", opener=opener)
        self.assertEqual(result.authentication, "accepted")
        self.assertEqual(result.target_model, "present")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.auth_header, "Bearer unit-test-key")

    def test_missing_and_invalid_credentials_do_not_send(self):
        opener = FakeOpener()
        self.assertEqual(run_once(key_reader=lambda: None, opener=opener).http_status, "not-sent")
        self.assertEqual(opener.calls, 0)

    def test_http_auth_errors(self):
        for status in (401, 403):
            opener = FakeOpener(error=urllib.error.HTTPError("http://test", status, "hidden", {}, io.BytesIO(b"secret")))
            result = run_once(key_reader=lambda: "key", opener=opener)
            self.assertEqual(result.authentication, "rejected")
            self.assertEqual(result.http_status, str(status))
            self.assertEqual(opener.calls, 1)

    def test_connection_timeout_and_invalid_json(self):
        opener = FakeOpener(error=TimeoutError())
        self.assertEqual(run_once(key_reader=lambda: "key", opener=opener).http_status, "connection-failed")
        opener = FakeOpener(FakeResponse(b"not-json"))
        result = run_once(key_reader=lambda: "key", opener=opener)
        self.assertEqual(result.target_model, "unknown")
        self.assertEqual(result.http_status, "200")


if __name__ == "__main__":
    unittest.main()
