import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "secure_secret_init.py"


def load_module():
    spec = importlib.util.spec_from_file_location("secure_secret_init_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SecretRuntimePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_private_copy_requests_mode_0400_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"x" * 32)
            with mock.patch.object(
                self.module, "_set_private_mode", wraps=self.module._set_private_mode
            ) as mode_mock, mock.patch.object(
                self.module.os, "replace", wraps=os.replace
            ) as replace_mock:
                self.module.install_private_secret(source, destination, temporary)
            mode_mock.assert_called_once_with(mock.ANY, temporary)
            replace_mock.assert_called_once_with(temporary, destination)
            self.assertTrue(destination.is_file())
            self.assertFalse(temporary.exists())

    def test_failed_atomic_replace_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"x" * 32)
            destination.write_bytes(b"existing")
            with mock.patch.object(
                self.module.os, "replace", side_effect=OSError("replace failed")
            ):
                with self.assertRaises(self.module.SecretInitializationError):
                    self.module.install_private_secret(source, destination, temporary)
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertFalse(temporary.exists())

    def test_write_failure_closes_descriptor_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"x" * 32)
            with mock.patch.object(
                self.module.os, "write", side_effect=OSError("write failed")
            ), mock.patch.object(
                self.module.os, "close", wraps=os.close
            ) as close_mock:
                with self.assertRaises(self.module.SecretInitializationError):
                    self.module.install_private_secret(source, destination, temporary)
            self.assertGreaterEqual(close_mock.call_count, 1)
            self.assertFalse(destination.exists())
            self.assertFalse(temporary.exists())

    def test_fchmod_failure_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"x" * 32)
            with mock.patch.object(
                self.module, "_set_private_mode", side_effect=OSError("fchmod failed")
            ):
                with self.assertRaises(self.module.SecretInitializationError):
                    self.module.install_private_secret(source, destination, temporary)
            self.assertFalse(destination.exists())
            self.assertFalse(temporary.exists())

    def test_posix_mode_path_calls_fchmod_when_available(self):
        temporary = Path("unused-test-path")
        with mock.patch.object(
            self.module.os, "fchmod", create=True
        ) as fchmod_mock:
            self.module._set_private_mode(123, temporary)
        fchmod_mock.assert_called_once_with(123, 0o400)

    def test_preexisting_temporary_file_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"x" * 32)
            temporary.write_bytes(b"preexisting")
            with self.assertRaises(self.module.SecretInitializationError):
                self.module.install_private_secret(source, destination, temporary)
            self.assertEqual(temporary.read_bytes(), b"preexisting")
            self.assertFalse(destination.exists())

    def test_cleanup_failure_does_not_disclose_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            destination = root / "destination"
            temporary = root / ".temporary"
            source.write_bytes(b"sensitive-test-value-123456")
            with mock.patch.object(
                self.module.os, "replace", side_effect=OSError("replace failed")
            ), mock.patch.object(
                self.module.Path, "unlink", side_effect=OSError("unlink failed")
            ):
                with self.assertRaises(self.module.SecretInitializationError) as raised:
                    self.module.install_private_secret(source, destination, temporary)
            self.assertEqual(
                str(raised.exception), "temporary credential cleanup failed"
            )

    def test_missing_or_invalid_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for source in (root / "missing", root / "invalid"):
                if source.name == "invalid":
                    source.write_bytes(b"bad\n")
                with self.subTest(source=source.name):
                    with self.assertRaises(self.module.SecretInitializationError):
                        self.module.install_private_secret(
                            source,
                            root / "destination",
                            root / ("temporary-" + source.name),
                        )

    def test_image_keeps_nonroot_runtime_and_no_secret_build_input(self):
        dockerfile = (ROOT / "Dockerfile.secure").read_text(encoding="utf-8")
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn("ENTRYPOINT", dockerfile)
        self.assertNotIn("ARG FISH", dockerfile)
        self.assertNotIn("ENV FISH_AUDIO_API_KEY", dockerfile)

    def test_compose_uses_private_parent_and_tmpfs_destination(self):
        for name in (
            "compose.production.yml",
            "compose.diagnostic.yml",
            "compose.validation.yml",
            "compose.secret-validation.yml",
            "compose.secrets.override.yml",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("/run/compose-secrets/fish_audio_api_key", text)
                self.assertIn("/run/ponkun-secrets/fish_audio_api_key", text)
                self.assertIn("mode=0700,uid=65532,gid=65532", text)
                self.assertNotIn("mode: 0400", text)

    def test_initializer_never_exports_or_reports_secret(self):
        text = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("os.environ[", text)
        self.assertNotIn("hashlib", text)
        self.assertNotIn("print(raw", text)
        self.assertNotIn("print(value", text)
        self.assertIn("os.execvp", text)


if __name__ == "__main__":
    unittest.main()
