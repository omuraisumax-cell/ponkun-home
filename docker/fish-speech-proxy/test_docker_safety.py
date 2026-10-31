import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class DockerSafetyTests(unittest.TestCase):
    def test_validation_compose_is_isolated_from_production(self):
        text = (ROOT / "compose.validation.yml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:18000:8000"', text)
        self.assertNotIn('"127.0.0.1:8000:8000"', text)
        self.assertNotIn("fish-speech-proxy:", text)
        self.assertNotIn("fish-speech-5090-api:", text)

    def test_secret_contract_matches_proxy(self):
        compose = (ROOT / "compose.validation.yml").read_text(encoding="utf-8")
        proxy = (ROOT / "tts_proxy.py").read_text(encoding="utf-8")
        self.assertIn("/run/compose-secrets/fish_audio_api_key", compose)
        self.assertIn("/run/ponkun-secrets/fish_audio_api_key", compose)
        self.assertIn("/run/ponkun-secrets/fish_audio_api_key", proxy)
        self.assertNotIn("FISH_API_KEY=", compose)

    def test_image_is_built_from_secure_source_not_retagged(self):
        compose = (ROOT / "compose.validation.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile.secure").read_text(encoding="utf-8")
        self.assertIn("build:", compose)
        self.assertIn("COPY tts_proxy.py", dockerfile)
        self.assertNotIn("fish-speech-5090", dockerfile)

    def test_diagnostic_compose_cannot_collide_with_running_validation(self):
        text = (ROOT / "compose.diagnostic.yml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:18001:8000"', text)
        self.assertNotIn('"127.0.0.1:18000:8000"', text)
        self.assertIn("ponkun-fish-cloud-proxy-diagnostic", text)
        self.assertNotIn("ponkun-fish-cloud-proxy-validation\n", text)


if __name__ == "__main__":
    unittest.main()
