import unittest

import ponkun_tts


class _ErrorResponse:
    status_code = 503
    content = b""
    headers = {}


class PonkunTTSLegacyPayloadTests(unittest.TestCase):
    def test_legacy_local_sends_openai_compatible_four_field_payload(self):
        captured = {}

        def request(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return _ErrorResponse()

        environment = {
            "PONKUN_TTS_MODEL": "test-model",
            "PONKUN_TTS_REFERENCE_ID": "test-reference",
            "PONKUN_TTS_FORMAT": "mp3",
        }

        with self.assertRaises(ponkun_tts.TTSAPIError):
            ponkun_tts.synthesize(
                " test speech ",
                backend=ponkun_tts.LEGACY_LOCAL,
                request_fn=request,
                environ=environment,
            )

        self.assertEqual(captured["url"], ponkun_tts.DEFAULT_LEGACY_URL)
        self.assertEqual(
            captured["json"],
            {
                "model": "test-model",
                "voice": "test-reference",
                "input": "test speech",
                "response_format": "mp3",
            },
        )


if __name__ == "__main__":
    unittest.main()
