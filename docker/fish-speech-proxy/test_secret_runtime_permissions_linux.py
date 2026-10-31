import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "secure_secret_init.py"
IS_LINUX = os.name == "posix" and sys.platform.startswith("linux")


def load_module():
    spec = importlib.util.spec_from_file_location("secure_secret_init_linux_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(IS_LINUX, "Linux POSIX permission verification requires Linux")
class LinuxPosixPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_private_copy_is_0400_and_parent_is_0700(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            private = root / "private"
            destination = private / "secret"
            temporary = private / ".secret.tmp"
            source.write_bytes(b"x" * 32)
            private.mkdir(mode=0o700)
            os.chmod(private, 0o700)
            self.module.install_private_secret(source, destination, temporary)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o400)


@unittest.skipUnless(
    IS_LINUX and hasattr(os, "getuid") and os.getuid() == 65532,
    "Ownership verification requires Linux running as UID 65532",
)
class LinuxRuntimeOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_private_copy_and_parent_are_owned_by_uid_65532(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            private = root / "private"
            destination = private / "secret"
            temporary = private / ".secret.tmp"
            source.write_bytes(b"x" * 32)
            private.mkdir(mode=0o700)
            self.module.install_private_secret(source, destination, temporary)
            self.assertEqual(private.stat().st_uid, 65532)
            self.assertEqual(destination.stat().st_uid, 65532)


if __name__ == "__main__":
    unittest.main()
