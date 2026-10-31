import unittest

from ponkun_hub.secure_credentials import (
    HUB_CLIENT_TARGET_NAME,
    CredentialError,
    hub_client_credential_status,
    read_hub_client_token,
    write_hub_client_token,
)


class HubCredentialManagerAdapterTests(unittest.TestCase):
    def test_missing_optional_token_returns_none(self):
        calls = []

        def reader(target):
            calls.append(target)
            return None

        self.assertIsNone(read_hub_client_token(reader=reader))
        self.assertEqual(calls, [HUB_CLIENT_TARGET_NAME])
        self.assertEqual(hub_client_credential_status(reader), "missing")

    def test_missing_required_token_fails_closed(self):
        with self.assertRaises(CredentialError):
            read_hub_client_token(reader=lambda _target: None, required=True)

    def test_present_token_is_validated_without_output(self):
        self.assertEqual(
            read_hub_client_token(reader=lambda _target: "mock-hub-token", required=True),
            "mock-hub-token",
        )
        self.assertEqual(
            hub_client_credential_status(lambda _target: "mock-hub-token"),
            "present-valid",
        )

    def test_invalid_token_status_does_not_expose_value(self):
        secret = " mock-hub-token "
        self.assertEqual(
            hub_client_credential_status(lambda _target: secret),
            "present-invalid",
        )
        with self.assertRaises(CredentialError) as caught:
            read_hub_client_token(reader=lambda _target: secret)
        self.assertNotIn(secret.strip(), str(caught.exception))

    def test_write_uses_dedicated_target_with_mock_writer(self):
        captured = []

        def writer(value, target):
            captured.append((value, target))

        write_hub_client_token("mock-hub-token", writer=writer)
        self.assertEqual(captured, [("mock-hub-token", HUB_CLIENT_TARGET_NAME)])


if __name__ == "__main__":
    unittest.main()
