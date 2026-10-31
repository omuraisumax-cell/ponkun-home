import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fish_api_key_gui as gui


VALID_FAKE = "x" * 32


class FishApiKeyGuiTests(unittest.TestCase):
    def test_missing_status(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(gui.credential_status(Path(folder) / "missing"), "missing")

    def test_valid_and_invalid_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_bytes(VALID_FAKE.encode("utf-8"))
            self.assertEqual(gui.credential_status(path), "present-valid")
            path.write_bytes(b"short")
            self.assertEqual(gui.credential_status(path), "present-invalid")

    def test_acl_failure_leaves_no_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "secret"
            with self.assertRaises(RuntimeError):
                gui.save_secret(VALID_FAKE, path, acl_setter=mock.Mock(side_effect=OSError()))
            self.assertFalse(path.exists())

    def test_atomic_update_replaces_content_without_newline_or_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "secret"
            path.parent.mkdir()
            path.write_bytes(("y" * 32).encode("utf-8"))
            gui.save_secret(VALID_FAKE, path, acl_setter=mock.Mock())
            self.assertEqual(path.read_bytes(), VALID_FAKE.encode("utf-8"))
            self.assertEqual(list(path.parent.glob(".fish-secret-*")), [])

    def test_default_location_is_outside_repository(self):
        path = gui.default_secret_path({"LOCALAPPDATA": "C:/Users/test/AppData/Local"})
        self.assertNotIn("jarvis-test", str(path).casefold())
        self.assertEqual(path.name, "fish_audio_api_key")

    def test_gui_source_never_prints_or_copies_secret(self):
        source = Path(gui.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("clipboard", source)
        self.assertNotIn("print(", source)
        self.assertIn('show="•"', source)


if __name__ == "__main__":
    unittest.main()
