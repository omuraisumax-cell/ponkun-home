"""Hidden, fixed launchers used by the Control Center.

No shell is used. Child output is consumed and only redacted error lines plus
lifecycle events are written to a local runtime log; normal conversation or
audio output is not persisted.
"""
from __future__ import annotations
import json

import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .secure_credentials import mobile_credential_status

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "ponkun_hub" / "control_center_settings.json"
PYTHON = Path(os.getenv("PONKUN_PYTHON", sys.executable))
LOG_DIR = ROOT / "ponkun_hub" / "runtime_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SECRET_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+\S+|(?:api[_ -]?key|token|authorization)\s*[:=]\s*\S+)")


def hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def hidden_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _write_log(service: str, message: str) -> None:
    safe = SECRET_RE.sub("[REDACTED]", message).replace("\x00", "")[:600]
    with (LOG_DIR / f"{service}.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {safe}\n")


def _capture_errors(service: str, stream) -> None:
    for line in iter(stream.readline, ""):
        lowered = line.casefold()
        if any(word in lowered for word in ("error", "exception", "traceback", "failed", "fatal")):
            _write_log(service, line.rstrip())
    stream.close()


def build_process_query(script_name: str) -> str:
    """Build the fixed WMI filter used for Mobile and PC process checks."""
    safe_name = script_name.replace("'", "''")
    return (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^python(w)?\\.exe$' "
        f"-and $_.CommandLine -like '*{safe_name}*' }} | "
        "Measure-Object | Select-Object -ExpandProperty Count"
    )


def _process_running(script_name: str) -> bool | None:
    if os.name != "nt":
        return None
    query = build_process_query(script_name)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=3, startupinfo=hidden_startupinfo(),
            creationflags=hidden_creationflags(), check=False,
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip() or "0") > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _env_mobile() -> dict[str, str]:
    env = os.environ.copy()
    vision_backend = "disabled"
    try:
        settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(settings, dict) and settings.get("vision_backend") == "openai":
            vision_backend = "openai"
    except (OSError, ValueError, TypeError):
        pass
    env.update({
        "PONKUN_MOBILE_AUTH_TOKEN": "",
        "PONKUN_STRATA_API_KEY": "",
        "PONKUN_OPENAI_API_KEY": "",
        "PONKUN_MOBILE_AUTH_REQUIRED": "1",
        "PONKUN_MOBILE_HUB_URL": "http://127.0.0.1:8788",
        "PONKUN_HUB_URL": "http://127.0.0.1:8788",
        "PONKUN_MOBILE_HUB_ONLY": "1",
        "PONKUN_INFERENCE_BACKEND": "strata",
        "PONKUN_OPENAI_WEB_ENABLED": "1",
        "PONKUN_OPENAI_WEB_MODEL": "gpt-4.1-mini",
        "PONKUN_VISION_BACKEND": vision_backend,
        "PONKUN_WEB_SEARCH_ENABLED": "0",
        "PYTHONPATH": f"{ROOT};{ROOT / '.venv' / 'Lib' / 'site-packages'};{ROOT / '.venv-gpu' / 'Lib' / 'site-packages'}",
    })
    return env


def _launch(service: str, command: list[str], env: dict[str, str]) -> int:
    _write_log(service, "start requested")
    try:
        process = subprocess.Popen(
            command, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            startupinfo=hidden_startupinfo(), creationflags=hidden_creationflags(),
        )
    except OSError as exc:
        _write_log(service, f"launch failed: {type(exc).__name__}")
        return 2
    threading.Thread(target=_capture_errors, args=(service, process.stdout), daemon=True).start()
    code = process.wait()
    _write_log(service, f"process exited code={code}")
    return code


def launch_mobile() -> int:
    if mobile_credential_status() != "present-valid":
        _write_log("mobile", "launch refused: Mobile credential is not present-valid")
        return 3
    running = _process_running("uvicorn ponkun_mobile:app")
    if running is None:
        _write_log("mobile", "launch refused: existing process ownership could not be checked")
        return 4
    if running:
        _write_log("mobile", "launch refused: Mobile process already exists")
        return 5
    return _launch("mobile", [str(PYTHON), "-B", "-m", "uvicorn", "ponkun_mobile:app", "--host", "127.0.0.1", "--port", "8002", "--log-level", "warning"], _env_mobile())


def launch_pc() -> int:
    running = _process_running("ponkun_pc.py")
    if running is None:
        _write_log("pc", "launch refused: PC process ownership could not be checked")
        return 4
    if running:
        _write_log("pc", "launch refused: PC process already exists")
        return 5
    env = os.environ.copy()
    env.update({"PONKUN_HUB_URL": "http://127.0.0.1:8788", "PONKUN_OPENAI_WEB_ENABLED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": f"{ROOT};{ROOT / '.venv' / 'Lib' / 'site-packages'}"})
    return _launch("pc", [str(PYTHON), "-B", str(ROOT / "ponkun_pc.py")], env)


def main(argv: list[str] | None = None) -> int:
    command = (argv or sys.argv[1:])
    if command == ["mobile"]:
        return launch_mobile()
    if command == ["pc"]:
        return launch_pc()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
