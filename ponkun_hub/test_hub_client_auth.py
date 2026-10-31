import unittest

from ponkun_hub.hub_client_auth import (
    HUB_CLIENT_TOKEN_TARGET,
    TOKEN_HEADER,
    HubClientTokenMismatch,
    HubClientTokenMissing,
    build_client_headers,
    build_component_headers,
    load_hub_client_token,
    validate_client_token,
)


class HubClientAuthTests(unittest.TestCase):
    def test_unset_required_token_fails_closed(self):
        requested = []

        def provider(target):
            requested.append(target)
            return None

        with self.assertRaises(HubClientTokenMissing):
            load_hub_client_token(provider, required=True)
        self.assertEqual(requested, [HUB_CLIENT_TOKEN_TARGET])

    def test_matching_token_builds_header(self):
        token = load_hub_client_token(
            lambda target: "mock-token" if target == HUB_CLIENT_TOKEN_TARGET else None,
            required=True,
        )
        headers = build_client_headers(token, required=True, base={"Content-Type": "application/json"})
        self.assertEqual(headers[TOKEN_HEADER], "mock-token")
        self.assertTrue(validate_client_token("mock-token", token, required=True))

    def test_mismatching_token_is_rejected(self):
        with self.assertRaises(HubClientTokenMismatch):
            validate_client_token("wrong-token", "expected-token", required=True)

    def test_missing_header_is_rejected_when_token_is_configured(self):
        with self.assertRaises(HubClientTokenMismatch):
            validate_client_token(None, "expected-token", required=True)

    def test_same_provider_supplies_sync_and_stream_headers_to_all_components(self):
        calls = []

        def provider(target):
            calls.append(target)
            return "shared-token"

        for component in ("hub", "mobile", "pc_voice"):
            for path in ("/api/inference", "/api/inference/stream"):
                headers = build_component_headers(
                    provider,
                    component=component,
                    path=path,
                    required=True,
                )
                self.assertEqual(headers[TOKEN_HEADER], "shared-token")
        self.assertEqual(calls, [HUB_CLIENT_TOKEN_TARGET] * 6)

    def test_optional_mode_does_not_send_header_when_provider_is_empty(self):
        headers = build_component_headers(
            lambda target: None,
            component="mobile",
            path="/api/inference",
            required=False,
            base={"Content-Type": "application/json"},
        )
        self.assertNotIn(TOKEN_HEADER, headers)


if __name__ == "__main__":
    unittest.main()
