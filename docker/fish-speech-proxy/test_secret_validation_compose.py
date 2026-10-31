import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "compose.secret-validation.yml"
GUIDE = ROOT / "SECRET_VALIDATION_18002.md"


class SecretValidationComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = COMPOSE.read_text(encoding="utf-8")
        cls.guide = GUIDE.read_text(encoding="utf-8")

    def test_uses_unique_project_container_image_and_loopback_port(self):
        self.assertIn("name: ponkun-fish-proxy-secret-validation-18002", self.compose)
        self.assertIn(
            "container_name: ponkun-fish-cloud-proxy-secret-validation-18002",
            self.compose,
        )
        self.assertIn(
            "image: ponkun-fish-cloud-proxy:secure-secret-validation-18002",
            self.compose,
        )
        self.assertIn('"127.0.0.1:18002:8000"', self.compose)
        self.assertNotIn("fish-speech-5090", self.compose)

    def test_builds_secure_nonroot_image_and_mounts_secret_read_only(self):
        self.assertIn("dockerfile: Dockerfile.secure", self.compose)
        self.assertIn('user: "65532:65532"', self.compose)
        self.assertIn("/run/compose-secrets/fish_audio_api_key", self.compose)
        self.assertIn("secrets:", self.compose)
        self.assertNotIn("volumes:", self.compose)
        self.assertIn("/run/ponkun-secrets/fish_audio_api_key", self.compose)
        self.assertIn("mode=0700,uid=65532,gid=65532", self.compose)

    def test_healthcheck_is_local_get_and_service_does_not_auto_restart(self):
        self.assertIn("http://127.0.0.1:8000/healthz", self.compose)
        self.assertIn('restart: "no"', self.compose)
        self.assertNotIn("/v1/audio/speech", self.compose)

    def test_guide_keeps_start_stop_explicit_and_checks_permissions(self):
        self.assertIn("config --quiet", self.guide)
        self.assertIn("up -d --no-build", self.guide)
        self.assertIn(" stop fish-cloud-proxy-secret-validation", self.guide)
        self.assertIn("--user 65533:65533", self.guide)
        self.assertIn("test ! -r /run/compose-secrets/fish_audio_api_key", self.guide)
        self.assertIn("test ! -r /run/ponkun-secrets/fish_audio_api_key", self.guide)
        self.assertNotIn("docker compose down", self.guide)

    def test_linux_tests_are_run_as_ephemeral_non_port_container(self):
        self.assertIn("run --rm --no-deps --entrypoint python", self.guide)
        self.assertIn("test_secret_runtime_permissions_linux", self.guide)


if __name__ == "__main__":
    unittest.main()
