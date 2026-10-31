import os
import sys
import unittest
from unittest.mock import patch

from ponkun_hub import background_launch


class BackgroundLaunchEncodingTests(unittest.TestCase):
    @patch("ponkun_hub.background_launch._write_log")
    def test_utf8_child_output_exits_cleanly(self, _write_log):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        code = background_launch._launch(
            "encoding-test",
            [sys.executable, "-c", "print('👏')"],
            env,
        )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
