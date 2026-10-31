import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import ponkun_openai_vision as vision


PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image"


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "合成画像のテスト回答です。"}]}]}).encode()

    def getcode(self):
        return 200


class VisionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PONKUN_VISION_BACKEND": "openai", "PONKUN_OPENAI_API_KEY": "test-only-dummy", "PONKUN_VISION_ENDPOINT": "https://example.invalid/v1/responses"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_one_mock_request_and_response(self):
        with patch("urllib.request.build_opener") as build:
            opener = build.return_value
            opener.open.return_value = _Response()
            answer, meta = vision.analyze_image("これは合成画像です。", PNG_1X1, "image/png")
        self.assertIn("合成画像", answer)
        self.assertEqual(meta["status"], 200)
        self.assertEqual(opener.open.call_count, 1)

    def test_webp_is_forwarded_in_memory(self):
        with patch("urllib.request.build_opener") as build:
            opener = build.return_value
            opener.open.return_value = _Response()
            answer, meta = vision.analyze_image("WebP合成画像", b"RIFFsyntheticWEBP", "image/webp")
        self.assertIn("合成画像", answer)
        self.assertEqual(meta["status"], 200)
        request = opener.open.call_args.args[0]
        self.assertIn(b"data:image/webp;base64,", request.data)

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"PONKUN_VISION_BACKEND": "disabled"}):
            with self.assertRaisesRegex(vision.OpenAIVisionError, "vision_disabled"):
                vision.analyze_image("x", PNG_1X1, "image/png")

    def test_provider_status_is_classified(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urllib.error.HTTPError("https://example.invalid", 401, "", {}, None)
            with self.assertRaisesRegex(vision.OpenAIVisionError, "vision_authentication_failed"):
                vision.analyze_image("x", PNG_1X1, "image/png")

    def test_no_key_is_classified_without_request(self):
        with patch.dict(os.environ, {"PONKUN_OPENAI_API_KEY": ""}):
            with patch("urllib.request.build_opener") as build:
                with patch.object(vision, "read_openai_key", None):
                    with self.assertRaisesRegex(vision.OpenAIVisionError, "vision_credential_missing"):
                        vision.analyze_image("x", PNG_1X1, "image/png")
                build.assert_not_called()

    def test_invalid_response_is_classified(self):
        response = _Response()
        response.read = lambda: b"not-json"
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(vision.OpenAIVisionError, "vision_invalid_response"):
                vision.analyze_image("x", PNG_1X1, "image/png")


if __name__ == "__main__":
    unittest.main()
