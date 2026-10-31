import unittest
from pathlib import Path
from unittest.mock import patch

import ponkun_hub.control_center as cc


class ControlCenterSafetyTests(unittest.TestCase):
    def test_http_state_mapping(self):
        self.assertEqual(cc.state_for_http(200), "Running")
        self.assertEqual(cc.state_for_http(0), "Stopped")
        self.assertEqual(cc.state_for_http(503), "Error")

    def test_start_policy(self):
        self.assertTrue(cc.start_enabled("hub", "Stopped"))
        self.assertTrue(cc.start_enabled("mobile", "Error"))
        self.assertFalse(cc.start_enabled("hub", "Running"))
        self.assertTrue(cc.start_enabled("pc", "Stopped"))
        self.assertFalse(cc.start_enabled("pc", "Unknown"))

    def test_hidden_process_flags_are_available(self):
        self.assertIsInstance(cc.hidden_creationflags(), int)
        self.assertEqual(cc.BACKGROUND_MODULE, "ponkun_hub.background_launch")

    def test_shared_process_query_is_python_only(self):
        mobile_query = cc.build_process_query("uvicorn ponkun_mobile:app")
        pc_query = cc.build_process_query("ponkun_pc.py")
        for query, marker in ((mobile_query, "uvicorn ponkun_mobile:app"), (pc_query, "ponkun_pc.py")):
            self.assertIn("Name -match '^python(w)?\\.exe$'", query)
            self.assertIn(marker, query)
            self.assertNotIn("powershell.exe", query)

    def test_status_colors_cover_user_states(self):
        for state in ("Running", "Stopped", "Starting", "Error", "Unknown"):
            self.assertIn(state, cc.COLORS)

    def test_toggle_is_canvas_based_and_large_enough(self):
        self.assertGreaterEqual(cc.ToggleSwitch.WIDTH, 64)
        self.assertGreaterEqual(cc.ToggleSwitch.HEIGHT, 36)
        source = Path(cc.__file__).read_text(encoding="utf-8")
        self.assertIn("class ToggleSwitch", source)
        self.assertIn("create_oval", source)

    def test_toggle_policy_keeps_running_services_on(self):
        self.assertFalse(cc.start_enabled("hub", "Running"))
        self.assertFalse(cc.start_enabled("mobile", "Ready"))
        self.assertFalse(cc.start_enabled("strata", "Running"))
        self.assertFalse(cc.start_enabled("pc", "Unknown"))

    def test_no_forced_stop_or_new_console(self):
        source = Path(cc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("taskkill /F", source.lower())
        self.assertNotIn("CREATE_NEW_CONSOLE", source)

    def test_fixed_targets_are_local_or_known(self):
        self.assertEqual(cc.HUB_URL, "http://127.0.0.1:8788")
        self.assertEqual(cc.STRATA_URL, "http://127.0.0.1:47831")
        self.assertEqual(cc.MOBILE_URL, "http://127.0.0.1:8002")
        self.assertEqual(cc.HUB_TASK, r"\PonkunHomeHubAutoStart")
        self.assertEqual(cc.startup_sequence(), ("hub", "strata", "mobile", "pc"))
        self.assertTrue(cc.MODEL_ID == "iq3_s")

    def test_default_settings_are_safe_and_non_secret(self):
        with patch.object(cc, "CONFIG_PATH") as config_path:
            config_path.read_text.side_effect = OSError
            settings = cc.load_settings()
        self.assertEqual(settings, {"auto_start_on_open": True, "auto_start_pc": True, "vision_backend": "disabled"})
        self.assertNotIn("key", repr(settings).lower())

    def test_launcher_paths_are_fixed(self):
        self.assertTrue(cc.PC_LAUNCHER.name == "start_ponkun_pc.cmd")
        self.assertTrue(cc.MOBILE_LAUNCHER.name == "start_mobile_local.cmd")
        self.assertTrue(cc.PC_LAUNCHER.is_file())
        self.assertTrue(cc.MOBILE_LAUNCHER.is_file())

    @patch("ponkun_hub.control_center._get_json", return_value=(200, {"ok": True, "inference": {"backend": "strata", "model": "iq3_s", "authentication_configured": True}}, "HTTP 200"))
    def test_probe_does_not_expose_secret_fields(self, _mock):
        snapshot = cc.dashboard_snapshot()
        self.assertEqual(snapshot["hub"]["state"], "Running")
        self.assertNotIn("api_key", repr(snapshot).lower())
        self.assertNotIn("cookie", repr(snapshot).lower())

    def test_no_gui_process_is_created_by_import(self):
        self.assertFalse(hasattr(cc, "_gui_process"))

    def test_start_failures_have_explicit_error_path(self):
        source = Path(cc.__file__).read_text(encoding="utf-8")
        self.assertIn("def _mark_failed", source)
        self.assertIn("last_errors", source)

    def test_strata_key_dialog_is_integrated_without_secret_literals(self):
        source = Path(cc.__file__).read_text(encoding="utf-8")
        self.assertIn("show_strata_key_dialog", source)
        self.assertIn("APIキー設定", source)
        self.assertIn("open_strata_key_dialog", source)
        self.assertNotIn("PONKUN_STRATA_API_KEY=", source)


if __name__ == "__main__":
    unittest.main()
