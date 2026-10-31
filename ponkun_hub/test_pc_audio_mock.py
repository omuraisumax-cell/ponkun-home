import importlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class _FakeWaveInput:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getnchannels(self):
        return 1

    def getsampwidth(self):
        return 2

    def getframerate(self):
        return 16000

    def setpos(self, _value):
        pass

    def readframes(self, _value):
        return b"\0\0" * 16


class _FakeWaveOutput:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def setnchannels(self, _value):
        pass

    def setsampwidth(self, _value):
        pass

    def setframerate(self, _value):
        pass

    def writeframes(self, _value):
        pass


class PcWakeAudioMockTests(unittest.TestCase):
    def test_wake_audio_initializes_before_load_and_plays_once(self):
        calls = []

        class FakeMusic:
            def load(self, _path, *_args):
                calls.append("load_wake" if isinstance(_path, str) else "load_tts")

            def play(self):
                calls.append("play")

            def get_busy(self):
                return False

        class FakeMixer:
            music = FakeMusic()

            @staticmethod
            def init():
                calls.append("init")

        fake_modules = {
            "numpy": types.ModuleType("numpy"),
            "sounddevice": types.ModuleType("sounddevice"),
            "faster_whisper": types.ModuleType("faster_whisper"),
            "ponkun_core": types.ModuleType("ponkun_core"),
            "ponkun_tts": types.ModuleType("ponkun_tts"),
        }
        fake_modules["pygame"] = types.ModuleType("pygame")
        fake_modules["pygame"].mixer = FakeMixer()
        fake_modules["faster_whisper"].WhisperModel = object
        fake_modules["ponkun_core"].ask_ponkun = lambda *_a, **_k: None
        fake_modules["ponkun_tts"].synthesize = lambda *_a, **_k: None

        old_modules = {name: sys.modules.get(name) for name in fake_modules}
        try:
            sys.modules.update(fake_modules)
            sys.path.insert(0, ".")
            pc = importlib.import_module("ponkun_pc")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp:
                temp_path = temp.name
            pc.WAKE_SOUND_FILE = temp_path
            with patch.object(pc.wave, "open", side_effect=[_FakeWaveInput(), _FakeWaveOutput()]):
                pc.play_wake_segment()
            pc.synthesize = lambda *_a, **_k: types.SimpleNamespace(elapsed_ms=1, audio=b"mock-mp3")
            pc.speak("mock response")
            self.assertEqual(calls, ["init", "load_wake", "play", "load_tts", "play"])
        finally:
            sys.modules.pop("ponkun_pc", None)
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            if "." in sys.path:
                sys.path.remove(".")


if __name__ == "__main__":
    unittest.main()
