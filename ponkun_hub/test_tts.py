import unittest

from ponkun_tts import (
    FISH_CLOUD,
    LEGACY_LOCAL,
    TTSAPIError,
    TTSConfigurationError,
    TTSError,
    synthesize,
)


def make_mp3(frame_count=12):
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, 417-byte frames.
    frame_header = bytes.fromhex("fffb9064")
    return (frame_header + (b"\x00" * 413)) * frame_count


def make_mp3_vbr_with_id3():
    """Synthetic VBR stream with ID3v2 and ID3v1 tags for parser tests."""
    frames = []
    for index in range(12):
        bitrate_index = 5 if index % 2 == 0 else 9  # 64 / 128 kbps
        header = (
            (0x7FF << 21) | (3 << 19) | (1 << 17) | (1 << 16)
            | (bitrate_index << 12) | (0 << 10) | (0 << 9) | 0x64
        )
        bitrate = {5: 64000, 9: 128000}[bitrate_index]
        frame_length = (144 * bitrate // 44100)
        frames.append(header.to_bytes(4, "big") + (b"\x00" * (frame_length - 4)))
    id3v2 = b"ID3\x04\x00\x00\x00\x00\x00\x05title"
    id3v1 = b"TAG" + (b"\x00" * 125)
    return id3v2 + b"".join(frames) + id3v1


class FakeResponse:
    def __init__(self, status_code=200, content=None, content_type="audio/mpeg", content_length=None):
        self.status_code = status_code
        self.content = make_mp3() if content is None else content
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)


class TTSAdapterTests(unittest.TestCase):
    def test_legacy_local_success(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        result = synthesize("テスト", backend=LEGACY_LOCAL, request_fn=post)
        self.assertEqual(result.audio, make_mp3())
        self.assertEqual(result.backend, LEGACY_LOCAL)
        self.assertEqual(calls[0][0], "http://localhost:8000/v1/audio/speech")
        self.assertEqual(calls[0][1]["json"]["input"], "テスト")

    def test_cloud_headers_and_reference_id(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        env = {
            "PONKUN_TTS_BACKEND": FISH_CLOUD,
            "FISH_API_KEY": "test-only-key",
            "PONKUN_TTS_REFERENCE_ID": "voice-test",
            "PONKUN_TTS_MODEL": "s2.1-pro-free",
        }
        result = synthesize("テスト", request_fn=post, environ=env)
        self.assertEqual(result.backend, FISH_CLOUD)
        self.assertEqual(calls[0][0], "https://api.fish.audio/v1/tts")
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer test-only-key")
        self.assertEqual(calls[0][1]["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(calls[0][1]["json"]["reference_id"], "voice-test")

    def test_cloud_key_missing_does_not_send(self):
        calls = []

        def post(*args, **kwargs):
            calls.append(True)
            return FakeResponse()

        with self.assertRaises(TTSConfigurationError):
            synthesize("テスト", backend=FISH_CLOUD, request_fn=post, environ={})
        self.assertEqual(calls, [])

    def test_http_error_statuses(self):
        for status in (401, 402, 429, 500):
            with self.subTest(status=status):
                with self.assertRaises(TTSAPIError) as ctx:
                    synthesize("テスト", request_fn=lambda *a, **k: FakeResponse(status_code=status))
                self.assertEqual(ctx.exception.status_code, status)

    def test_timeout_is_bounded_error(self):
        def post(*args, **kwargs):
            raise __import__("requests").Timeout()

        with self.assertRaises(TTSError):
            synthesize("テスト", request_fn=post, timeout=1)

    def test_empty_audio_rejected(self):
        with self.assertRaises(TTSAPIError):
            synthesize("テスト", request_fn=lambda *a, **k: FakeResponse(content=b""))

    def test_short_mp3_rejected(self):
        with self.assertRaises(TTSAPIError):
            synthesize("テスト", request_fn=lambda *a, **k: FakeResponse(content=make_mp3(2)))

    def test_content_length_mismatch_rejected(self):
        with self.assertRaises(TTSAPIError):
            synthesize(
                "テスト",
                request_fn=lambda *a, **k: FakeResponse(
                    content=make_mp3(), content_length=len(make_mp3()) + 1
                ),
            )

    def test_mp3_duration_is_reported(self):
        result = synthesize("テスト", request_fn=lambda *a, **k: FakeResponse())
        self.assertIsNotNone(result.duration_seconds)
        self.assertGreater(result.duration_seconds, 0.25)

    def test_vbr_mp3_with_id3_tags_is_accepted(self):
        result = synthesize(
            "テスト",
            request_fn=lambda *a, **k: FakeResponse(content=make_mp3_vbr_with_id3()),
        )
        self.assertGreater(result.duration_seconds, 0.25)

    def test_non_audio_content_type_rejected(self):
        with self.assertRaises(TTSAPIError):
            synthesize("テスト", request_fn=lambda *a, **k: FakeResponse(content=b"{}", content_type="application/json"))

    def test_empty_text_rejected_without_send(self):
        calls = []
        with self.assertRaises(TTSConfigurationError):
            synthesize("  ", request_fn=lambda *a, **k: calls.append(True))
        self.assertEqual(calls, [])

    def test_pc_and_mobile_use_shared_adapter_symbol(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        pc = (root / "ponkun_pc.py").read_text(encoding="utf-8-sig")
        mobile = (root / "ponkun_mobile.py").read_text(encoding="utf-8-sig")
        self.assertIn("from ponkun_tts import synthesize", pc)
        self.assertIn("from ponkun_tts import synthesize", mobile)
        self.assertIn("result = synthesize(text)", pc)
        self.assertIn("result = synthesize(text)", mobile)


if __name__ == "__main__":
    unittest.main()
