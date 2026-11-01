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

    def test_mobile_ui_keeps_actions_and_uses_modern_design_tokens(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        for element_id in ("sidebar", "historyList", "chat-scroller", "message", "cameraFile", "voiceFile"):
            self.assertIn(f'id="{element_id}"', source)
        self.assertIn("--accent-2:", source)
        self.assertIn("--safe-bottom: env(safe-area-inset-bottom", source)
        self.assertIn('id="welcomePanel"', source)
        self.assertIn('class="suggestion-grid"', source)
        self.assertIn('onclick="sendMessage()"', source)
        self.assertIn('savedImageInput.id = "imageFile"', source)
        self.assertIn('<textarea id="message" aria-label="メッセージ"', source)
        self.assertNotIn('placeholder="ぽんくんにメッセージを送る..."', source)

    def test_mobile_ui_shows_single_transient_thinking_indicator(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn('id = "thinkingIndicator"', source)
        self.assertIn('label.textContent = "ぽんくんが考えています..."', source)
        self.assertIn('className = "thinking-spinner"', source)
        self.assertIn("@keyframes thinking-spin", source)
        self.assertIn('if (document.getElementById("thinkingIndicator")) return', source)
        self.assertIn("let messageRequestInFlight = false", source)
        self.assertIn("messageRequestInFlight) return", source)
        self.assertGreaterEqual(source.count("finally {"), 2)
        self.assertGreaterEqual(source.count("hideThinkingIndicator();"), 4)

    def test_mobile_ui_supports_autogrow_and_local_avatar_customization(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn("resize: none", source)
        self.assertIn("scrollbar-width: none", source)
        self.assertIn("function resizeMessageInput()", source)
        self.assertIn('e.key === "Enter" && !e.shiftKey', source)
        for element_id in ("brandAvatar", "welcomeAvatar", "appearanceModal", "avatarPreview", "avatarFile"):
            self.assertIn(f'id="{element_id}"', source)
        self.assertIn('const AVATAR_STORAGE_KEY = "ponkun_mobile_avatar_v1"', source)
        self.assertIn("localStorage.setItem(AVATAR_STORAGE_KEY", source)
        self.assertIn("localStorage.removeItem(AVATAR_STORAGE_KEY", source)
        self.assertIn("reader.readAsDataURL(file)", source)

    def test_mobile_sidebar_overlays_content_without_horizontal_overflow(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn('content="width=device-width, initial-scale=1.0', source)
        self.assertIn("@media (max-width: 768px)", source)
        self.assertIn("width: 100vw", source)
        self.assertIn("transform: translateX(-100%)", source)
        self.assertIn("max-width: calc(100vw - 44px)", source)
        self.assertIn("overflow-x: hidden", source)
        self.assertIn('id="menuTrigger"', source)
        self.assertIn('aria-controls="sidebar"', source)
        self.assertIn('aria-expanded="false"', source)
        self.assertIn("function setSidebarOpen(open)", source)
        self.assertIn("function closeSidebar()", source)
        self.assertIn('if (event.key === "Escape") closeSidebar()', source)
        self.assertIn('onclick="closeSidebar()"', source)

    def test_mobile_stylesheet_has_balanced_blocks(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        stylesheet = source.split("<style>", 1)[1].split("</style>", 1)[0]
        depth = 0
        for character in stylesheet:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                self.assertGreaterEqual(depth, 0, "stylesheet contains an unmatched closing brace")
        self.assertEqual(depth, 0, "stylesheet contains an unclosed block")

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
