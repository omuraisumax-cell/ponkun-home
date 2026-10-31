import ast
import unittest
from pathlib import Path
import ponkun_hub.background_launch as background_launch


class MobileInitializationTests(unittest.TestCase):
    def test_whisper_constructor_is_lazy(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "WhisperModel"]
        self.assertEqual(len(calls), 1)
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "get_whisper_model")
        self.assertTrue(any(node in ast.walk(function) for node in calls))

    def test_vision_upload_requires_auth_csrf_and_consent(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/vision")', source)
        self.assertIn("Depends(require_auth)", source)
        self.assertIn('external_consent: str = Form("")', source)
        self.assertIn('external_consent != "1"', source)
        self.assertIn("validate_vision_image", source)

    def test_vision_ui_requires_explicit_consent_and_reports_provider_errors(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn("window.confirm", source)
        self.assertIn('formData.append("external_consent", "1")', source)
        self.assertIn("if (!approved) return", source)
        self.assertIn("diagnostic_id", source)

    def test_control_center_mobile_launch_propagates_vision_setting(self):
        source = Path(__file__).resolve().parent.joinpath("background_launch.py").read_text(encoding="utf-8")
        self.assertIn("PONKUN_VISION_BACKEND", source)
        self.assertIn('vision_backend = "disabled"', source)
        self.assertIn('settings.get("vision_backend") == "openai"', source)

    def test_mobile_process_probe_targets_python_only(self):
        query = background_launch.build_process_query("uvicorn ponkun_mobile:app")
        self.assertIn("Name -match '^python(w)?\\.exe$'", query)
        self.assertIn("uvicorn ponkun_mobile:app", query)
        self.assertNotIn("powershell.exe", query)

    def test_shared_process_probe_query_covers_pc_voice(self):
        query = background_launch.build_process_query("ponkun_pc.py")
        self.assertIn("Name -match '^python(w)?\\.exe$'", query)
        self.assertIn("ponkun_pc.py", query)
        self.assertNotIn("powershell.exe", query)


if __name__ == "__main__":
    unittest.main()
