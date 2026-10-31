"""Ponkun Home local Windows control center.

The UI is intentionally read-only for status and only starts fixed, existing
launchers. It never accepts shell commands, manages credentials, or stops a
service when the owner cannot be proven.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from .secure_credentials import CredentialError, openai_credential_status, read_strata_key
    from .background_launch import build_process_query
    from .strata_key_dialog import show_strata_key_dialog
except ImportError:
    from ponkun_hub.secure_credentials import CredentialError, openai_credential_status, read_strata_key
    from ponkun_hub.background_launch import build_process_query
    from ponkun_hub.strata_key_dialog import show_strata_key_dialog

ROOT = Path(__file__).resolve().parent.parent
HUB_URL = "http://127.0.0.1:8788"
STRATA_URL = "http://127.0.0.1:47831"
MOBILE_URL = "http://127.0.0.1:8002"
MOBILE_TAILSCALE_URL = os.getenv("PONKUN_MOBILE_REMOTE_URL", "")
PC_LAUNCHER = ROOT / "start_ponkun_pc.cmd"
MOBILE_LAUNCHER = ROOT / "ponkun_hub" / "start_mobile_local.cmd"
PYTHON = Path(os.getenv("PONKUN_PYTHON", sys.executable))
PYTHONW = PYTHON.with_name("pythonw.exe")
BACKGROUND_MODULE = "ponkun_hub.background_launch"
HUB_TASK = r"\PonkunHomeHubAutoStart"
CONFIG_PATH = ROOT / "ponkun_hub" / "control_center_settings.json"
MODEL_ID = "iq3_s"

COLORS = {
    "Running": "#59d98e", "Ready": "#59d98e", "Stopped": "#8b96a8",
    "Error": "#ff6e70", "Unknown": "#f0bd4f", "Starting": "#e0aa45", "Not responding": "#ff6e70",
    "Not ready": "#f0bd4f",
}


class ToggleSwitch:
    """Canvas-based iOS-style state indicator with no image dependency."""

    WIDTH = 76
    HEIGHT = 42

    def __init__(self, parent: tk.Misc, command=None) -> None:
        self.command = command
        self.canvas = tk.Canvas(parent, width=self.WIDTH, height=self.HEIGHT, highlightthickness=0, bd=0, bg="#17202d")
        self.canvas.pack(side="left", padx=(0, 8))
        self.enabled = False
        self.state = "Unknown"
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Return>", self._click)
        self.canvas.configure(takefocus=1)
        self.set_state("Unknown", False)

    def _round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> None:
        self.canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, **kwargs)
        self.canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, **kwargs)
        self.canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, **kwargs)
        self.canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, **kwargs)
        self.canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, **kwargs)
        self.canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, **kwargs)

    def set_state(self, state: str, enabled: bool) -> None:
        self.state = state
        self.enabled = enabled
        self.canvas.delete("all")
        if state in {"Running", "Ready"}:
            track, knob_x = "#45d391", 55
        elif state in {"Starting"}:
            track, knob_x = "#e0aa45", 31
        elif state in {"Stopped", "Failed", "Error"}:
            track, knob_x = "#667386", 21
        else:
            track, knob_x = "#465263", 31
        self._round_rect(3, 4, 73, 38, 16, fill=track, outline=track)
        self.canvas.create_oval(knob_x - 12, 9, knob_x + 12, 33, fill="#ffffff", outline="#d9e2ef", width=1)
        if enabled:
            self.canvas.configure(cursor="hand2")
        else:
            self.canvas.configure(cursor="arrow")

    def _click(self, _event=None) -> None:
        if self.enabled and self.command:
            self.command()


def state_for_http(status: int) -> str:
    if status == 200:
        return "Running"
    if status == 0:
        return "Stopped"
    return "Error"


def start_enabled(service: str, state: str) -> bool:
    """Return whether a fixed start action is safe for the current state."""
    if service == "pc":
        return state == "Stopped"  # only after the fixed process query confirms absence
    return state in {"Stopped", "Error"}


def _get_json(url: str, timeout: float = 1.5, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any] | None, str]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
            return int(response.status), body if isinstance(body, dict) else None, "HTTP 200"
    except urllib.error.HTTPError as exc:
        return int(exc.code), None, f"HTTP {exc.code}"
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return 0, None, type(exc).__name__


def probe_http(url: str) -> dict[str, Any]:
    status, body, detail = _get_json(url)
    return {"state": state_for_http(status), "status_code": status, "detail": detail, "body": body}


def probe_strata() -> dict[str, Any]:
    """Check API availability and model readiness without exposing the key."""
    result = probe_http(f"{STRATA_URL}/health")
    if result["status_code"] != 200:
        result["readiness"] = "Unknown"
        return result
    try:
        key = read_strata_key()
    except (CredentialError, OSError):
        key = None
    if not key:
        result["readiness"] = "Unknown"
        result["detail"] = "API応答あり / 資格情報未確認"
        return result
    try:
        status, body, detail = _get_json(
            f"{STRATA_URL}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
    finally:
        key = ""
    result["models_status"] = status
    result["models_detail"] = detail
    models = (body or {}).get("data", []) if isinstance(body, dict) else []
    ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
    result["model_id"] = MODEL_ID if MODEL_ID in ids else (next(iter(ids), "Unknown"))
    result["readiness"] = "Ready" if MODEL_ID in ids else "Not ready"
    return result


def probe_mobile() -> dict[str, Any]:
    result = probe_http(f"{MOBILE_URL}/login")
    if result["status_code"] in {200, 401, 403}:
        result["state"] = "Running"
        result["detail"] = f"HTTP {result['status_code']}（認証状態は表示しません）"
    return result


def hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def hidden_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pc_process_state() -> tuple[str, str]:
    """Use a fixed WMI query; unknown means the PC start control is disabled."""
    if os.name != "nt":
        return "Unknown", "Windowsプロセス確認はWindows上でのみ利用可能"
    query = build_process_query("ponkun_pc.py")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=3, startupinfo=hidden_startupinfo(),
            creationflags=hidden_creationflags(), check=False,
        )
        if result.returncode != 0:
            return "Unknown", "PC実プロセスの確認に失敗"
        return ("Running", "マイク監視プロセスを確認") if int(result.stdout.strip() or "0") > 0 else ("Stopped", "PC音声プロセスなし")
    except (OSError, subprocess.SubprocessError, ValueError):
        return "Unknown", "PC実プロセスの確認に失敗"


def dashboard_snapshot() -> dict[str, dict[str, Any]]:
    hub = probe_http(f"{HUB_URL}/healthz")
    inference = (hub.get("body") or {}).get("inference", {})
    hub["details"] = {
        "endpoint": inference.get("endpoint", "未取得"),
        "model": inference.get("model", "未取得"),
        "backend": inference.get("backend", "未取得"),
        "auth_configured": bool(inference.get("authentication_configured")),
    }

    strata = probe_strata()
    strata["state"] = "Running" if strata["status_code"] == 200 else "Not responding"

    mobile = probe_mobile()
    return {
        "hub": hub,
        "mobile": mobile,
        "strata": strata,
        "pc": dict(zip(("state", "detail"), pc_process_state())),
        "tailscale": {"state": "Unknown", "detail": "URL設定済み / 接続状態未確認"},
    }


def load_settings() -> dict[str, object]:
    defaults: dict[str, object] = {"auto_start_on_open": True, "auto_start_pc": True, "vision_backend": "disabled"}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in defaults:
                if key == "vision_backend" and data.get(key) in {"disabled", "openai"}:
                    defaults[key] = data[key]
                elif key != "vision_backend" and isinstance(data.get(key), bool):
                    defaults[key] = data[key]
    except (OSError, ValueError):
        pass
    return defaults


def save_settings(settings: dict[str, object]) -> None:
    safe = {
        "auto_start_on_open": bool(settings.get("auto_start_on_open", True)),
        "auto_start_pc": bool(settings.get("auto_start_pc", True)),
        "vision_backend": "openai" if settings.get("vision_backend") == "openai" else "disabled",
    }
    CONFIG_PATH.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def startup_sequence() -> tuple[str, ...]:
    """Documented dependency order used by the one-click coordinator."""
    return ("hub", "strata", "mobile", "pc")


class ControlCenter:
    SERVICE_INFO = {
        "hub": ("◈", "Ponkun Hub", "127.0.0.1:8788", "Qwenへの共通推論ゲートウェイ"),
        "mobile": ("▣", "Ponkun Mobile", "127.0.0.1:8002", "Galaxy PWAクライアント"),
        "strata": ("◆", "Strata / Qwen", "127.0.0.1:47831", "APIとモデルReadyを別々に確認"),
        "pc": ("◉", "PC Voice", "Clap → Whisper → Qwen → Fish", "PC音声版。外部プロセスは安全確認が必要"),
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Ponkun Home Control Center")
        self.root.geometry("1000x720")
        self.root.minsize(760, 560)
        self.root.resizable(False, False)
        self.root.configure(bg="#0d111a")
        self.settings = load_settings()
        self.auto_start_running = False
        self.initial_check_done = False
        self.pending: dict[str, str] = {}
        self.owned: dict[str, subprocess.Popen[str]] = {}
        self.card_state: dict[str, tk.Label] = {}
        self.switches: dict[str, ToggleSwitch] = {}
        self.card_detail: dict[str, tk.Label] = {}
        self.start_buttons: dict[str, ttk.Button] = {}
        self.log_text: tk.Text | None = None
        self.last_states: dict[str, str] = {}
        self.last_errors: dict[str, str] = {}
        self.overall_label: tk.Label | None = None
        self._strata_key_dialog: tk.Toplevel | None = None
        self._build()
        self.log("GUI起動")
        self.refresh()

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7), background="#26354a", foreground="#f5f7fb")
        style.map("TButton", background=[("active", "#34516e"), ("disabled", "#1b2431")])

        header = tk.Frame(self.root, bg="#0d111a")
        header.pack(fill="x", padx=22, pady=(16, 8))
        left = tk.Frame(header, bg="#0d111a")
        left.pack(side="left")
        tk.Label(left, text="Ponkun Home", font=("Segoe UI", 22, "bold"), bg="#0d111a", fg="#f7f9fc").pack(anchor="w")
        tk.Label(left, text="Control Center  /  ローカル専用", font=("Segoe UI", 10), bg="#0d111a", fg="#94a4b8").pack(anchor="w")
        self.overall_label = tk.Label(header, text="SYSTEM UNKNOWN", font=("Segoe UI", 11, "bold"), bg="#263244", fg=COLORS["Unknown"], padx=14, pady=7)
        self.overall_label.pack(side="right", padx=(10, 0))
        actions = tk.Frame(header, bg="#0d111a")
        actions.pack(side="right", anchor="s")
        ttk.Button(actions, text="すべて起動", command=self.start_all).pack(side="left", padx=4)
        ttk.Button(actions, text="⟳ すべて再チェック", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(actions, text="Hub Dashboard", command=lambda: webbrowser.open(f"{HUB_URL}/dashboard")).pack(side="left", padx=4)
        ttk.Button(actions, text="Mobileを開く", command=lambda: webbrowser.open(f"{MOBILE_URL}/login")).pack(side="left", padx=4)
        ttk.Button(actions, text="設定", command=self.show_settings).pack(side="left", padx=4)

        grid = tk.Frame(self.root, bg="#0d111a")
        grid.pack(fill="x", padx=16, pady=(4, 8))
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)
        for row in range(2):
            grid.grid_rowconfigure(row, minsize=205)
        for index, key in enumerate(("hub", "mobile", "strata", "pc")):
            self._make_card(grid, key, index // 2, index % 2)

        ts = tk.Frame(self.root, bg="#172231", highlightthickness=1, highlightbackground="#2c4057")
        ts.pack(fill="x", padx=22, pady=(0, 8))
        tk.Label(ts, text="🔗  Tailscale / Galaxy", font=("Segoe UI", 12, "bold"), bg="#172231", fg="#f4f7fb").grid(row=0, column=0, sticky="w", padx=14, pady=(9, 0))
        tk.Label(ts, text="HTTPS 8443 → 127.0.0.1:8002  |  URL設定済み / 接続状態未確認", bg="#172231", fg="#aab8ca").grid(row=1, column=0, sticky="w", padx=14, pady=(2, 9))
        ttk.Button(ts, text="URLをコピー", command=self.copy_tailscale_url).grid(row=0, column=1, rowspan=2, padx=10, pady=8)
        ttk.Button(ts, text="Galaxy URLを開く", command=lambda: webbrowser.open(MOBILE_TAILSCALE_URL)).grid(row=0, column=2, rowspan=2, padx=(0, 12), pady=8)
        ts.grid_columnconfigure(0, weight=1)

        log_frame = tk.Frame(self.root, bg="#0d111a")
        log_frame.pack(fill="x", padx=22, pady=(0, 14))
        tk.Label(log_frame, text="Activity Log", bg="#0d111a", fg="#9eacbf", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=4, bg="#111925", fg="#b9c5d5", insertbackground="#fff", relief="flat", state="disabled", font=("Consolas", 9), wrap="word")
        self.log_text.pack(fill="x", pady=(4, 0))

    def _make_card(self, parent: tk.Frame, key: str, row: int, column: int) -> None:
        icon, title, address, description = self.SERVICE_INFO[key]
        card = tk.Frame(parent, bg="#17202d", highlightthickness=1, highlightbackground="#2a3a4e")
        card.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
        card.configure(width=460, height=205)
        card.grid_propagate(False)
        top = tk.Frame(card, bg="#17202d")
        top.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(top, text=f"{icon}  {title}", font=("Segoe UI", 14, "bold"), bg="#17202d", fg="#f7f9fc").pack(side="left")
        switch_area = tk.Frame(top, bg="#17202d")
        switch_area.pack(side="right")
        switch = ToggleSwitch(switch_area, command=lambda service=key: self.handle_toggle(service))
        self.switches[key] = switch
        state = tk.Label(switch_area, text="Unknown", font=("Segoe UI", 10, "bold"), bg="#17202d", fg=COLORS["Unknown"])
        state.pack(side="left")
        tk.Label(card, text=address, font=("Consolas", 9), bg="#17202d", fg="#8fa7c2").pack(anchor="w", padx=14)
        tk.Label(card, text=description, bg="#17202d", fg="#aebacd", wraplength=420, justify="left").pack(anchor="w", padx=14, pady=(4, 0))
        detail = tk.Label(card, text="確認中…", bg="#17202d", fg="#d0d7e2", wraplength=420, justify="left", anchor="w")
        detail.pack(fill="x", padx=14, pady=(8, 8))
        self.card_state[key] = state
        self.card_detail[key] = detail
        button_row = tk.Frame(card, bg="#17202d")
        button_row.pack(fill="x", padx=10, pady=(0, 11))
        if key == "hub":
            self.start_buttons[key] = ttk.Button(button_row, text="Hubを起動", command=self.start_hub)
            self.start_buttons[key].pack(side="left", padx=4)
            ttk.Button(button_row, text="管理画面", command=lambda: webbrowser.open(f"{HUB_URL}/dashboard")).pack(side="left", padx=4)
        elif key == "mobile":
            self.start_buttons[key] = ttk.Button(button_row, text="Mobileを起動", command=self.start_mobile)
            self.start_buttons[key].pack(side="left", padx=4)
            ttk.Button(button_row, text="ブラウザで開く", command=lambda: webbrowser.open(f"{MOBILE_URL}/login")).pack(side="left", padx=4)
        elif key == "strata":
            ttk.Button(button_row, text="Healthを再確認", command=self.refresh).pack(side="left", padx=4)
            ttk.Button(button_row, text="APIキー設定", command=self.open_strata_key_dialog).pack(side="left", padx=4)
            tk.Label(button_row, text="停止・モデル操作なし", bg="#17202d", fg="#7f8da0").pack(side="left", padx=8)
        else:
            self.start_buttons[key] = ttk.Button(button_row, text="PC音声版を起動", command=self.start_pc)
            self.start_buttons[key].pack(side="left", padx=4)
            tk.Label(button_row, text="外部プロセスの所有確認が必要", bg="#17202d", fg="#f0bd4f").pack(side="left", padx=8)

    def log(self, text: str) -> None:
        if not self.log_text:
            return
        from datetime import datetime
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {text}\n")
        lines = self.log_text.get("1.0", "end-1c").splitlines()[-30:]
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(lines) + ("\n" if lines else ""))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh(self) -> None:
        self.log("状態更新を開始")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        snapshot = dashboard_snapshot()
        mobile_process = self.owned.get("mobile")
        if mobile_process and mobile_process.poll() is None and snapshot["mobile"]["state"] == "Stopped":
            snapshot["mobile"]["detail"] = "起動指示済み。HTTP状態を再確認中"
        self.root.after(0, lambda: self._render(snapshot))

    def _render(self, snapshot: dict[str, dict[str, Any]]) -> None:
        states: list[str] = []
        for key, item in snapshot.items():
            state_value = str(item.get("state", "Unknown"))
            if key in self.pending and state_value == "Stopped":
                state_value = "Starting"
            if key in self.last_errors and state_value == "Stopped":
                state_value = "Error"
            state = self.card_state[key]
            switch_text = "● ON" if state_value in {"Running", "Ready"} else ("… STARTING" if state_value == "Starting" else "● OFF")
            toggle_enabled = key != "strata" and start_enabled(key, state_value) and key not in self.owned and not self.auto_start_running
            self.switches[key].set_state(state_value, toggle_enabled)
            state.configure(text=state_value, fg=COLORS.get(state_value, COLORS["Unknown"]))
            details = item.get("details", item.get("detail", ""))
            if key in self.last_errors:
                details = self.last_errors[key]
            if isinstance(details, dict):
                details = "  |  ".join(f"{k}: {v}" for k, v in details.items())
            if key == "strata":
                details = f"API: {state_value}  /  model Ready: {item.get('readiness', 'Unknown')}  /  {details}"
            self.card_detail[key].configure(text=str(details))
            if key in self.owned and self.owned[key].poll() is not None:
                self.owned.pop(key, None)
            if key in self.pending and state_value not in {"Stopped", "Starting"}:
                self.pending.pop(key, None)
            if key in self.start_buttons:
                enabled = start_enabled(key, state_value) and key not in self.owned
                self.start_buttons[key].configure(state="normal" if enabled else "disabled")
            previous = self.last_states.get(key)
            if previous and previous != state_value:
                self.log(f"{self.SERVICE_INFO[key][1]}: {previous} → {state_value}")
            self.last_states[key] = state_value
            states.append(state_value)
        if self.overall_label:
            if any(state in {"Error", "Not responding"} for state in states):
                self.overall_label.configure(text="SYSTEM ATTENTION", fg=COLORS["Error"])
            elif all(state in {"Running", "Ready"} for state in states[:4]):
                self.overall_label.configure(text="SYSTEM ONLINE", fg=COLORS["Running"])
            else:
                self.overall_label.configure(text="SYSTEM CHECKING", fg=COLORS["Unknown"])
        self.log("状態更新完了")
        if not self.initial_check_done:
            self.initial_check_done = True
            if self.settings.get("auto_start_on_open", True):
                self.root.after(250, self.start_all)

    def _mark_starting(self, key: str, title: str) -> None:
        self.pending[key] = "Starting"
        self.last_errors.pop(key, None)
        if key in self.switches:
            self.switches[key].set_state("Starting", False)
        if key in self.card_state:
            self.card_state[key].configure(text="… STARTING", fg=COLORS["Unknown"])
        if key in self.start_buttons:
            self.start_buttons[key].configure(state="disabled")
        self.log(f"{title} Starting...")

    def _mark_failed(self, key: str, title: str, reason: str) -> None:
        self.pending.pop(key, None)
        self.last_errors[key] = reason
        self.last_states[key] = "Error"
        if key in self.card_state:
            self.card_state[key].configure(text="Error", fg=COLORS["Error"])
        if key in self.switches:
            self.switches[key].set_state("Error", False)
        self.log(f"{title} Failed: {reason}")

    def _wait_for(self, check, timeout: float = 45.0) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.5)
        return False

    def _request_hub_hidden(self) -> None:
        subprocess.Popen(
            ["schtasks.exe", "/Run", "/TN", HUB_TASK], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=hidden_startupinfo(), creationflags=hidden_creationflags(),
        )

    def _launch_background(self, key: str) -> subprocess.Popen[str] | None:
        interpreter = PYTHONW if PYTHONW.is_file() else PYTHON
        if not interpreter.is_file():
            return None
        try:
            process = subprocess.Popen(
                [str(interpreter), "-B", "-m", BACKGROUND_MODULE, key], cwd=str(ROOT),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(), creationflags=hidden_creationflags(), text=True,
            )
            self.owned[key] = process
            return process
        except OSError:
            return None

    def start_all(self) -> None:
        if self.auto_start_running:
            self.log("すべて起動を無視: 起動処理中")
            return
        self.auto_start_running = True
        self.log("一括起動を開始: Hub → Strata Ready → Mobile → PC")
        threading.Thread(target=self._start_all_worker, daemon=True).start()

    def _start_all_worker(self) -> None:
        try:
            snapshot = dashboard_snapshot()
            if snapshot["hub"]["state"] != "Running":
                self.root.after(0, lambda: self._mark_starting("hub", "Hub"))
                self._request_hub_hidden()
                if not self._wait_for(lambda: probe_http(f"{HUB_URL}/healthz")["state"] == "Running"):
                    self.root.after(0, lambda: self._mark_failed("hub", "Hub", "/healthz応答なし"))
                    return

            strata = probe_strata()
            if strata.get("state") != "Running" or strata.get("readiness") != "Ready":
                self.root.after(0, lambda: self.log("Strata保留: Desktopでiq3_sをReadyにしてください"))
                return

            mobile = probe_mobile()
            if mobile["state"] != "Running":
                self.root.after(0, lambda: self._mark_starting("mobile", "Mobile"))
                if self._launch_background("mobile") is None or not self._wait_for(lambda: probe_mobile()["state"] == "Running"):
                    self.root.after(0, lambda: self._mark_failed("mobile", "Mobile", "8002番のHTTP応答なし"))
                    return

            if self.settings.get("auto_start_pc", True):
                pc_state, _ = pc_process_state()
                if pc_state == "Stopped":
                    self.root.after(0, lambda: self._mark_starting("pc", "PC Voice"))
                    if self._launch_background("pc") is None or not self._wait_for(lambda: pc_process_state()[0] == "Running", timeout=30):
                        self.root.after(0, lambda: self._mark_failed("pc", "PC Voice", "マイク監視プロセスを確認できません"))
                        return
                elif pc_state == "Unknown":
                    self.root.after(0, lambda: self.log("PC Voice保留: プロセス所有を確認できません"))
                    return
            self.root.after(0, lambda: self.log("一括起動完了: 実状態を再確認します"))
        finally:
            self.auto_start_running = False
            self.root.after(0, self.pending.clear)
            self.root.after(0, self.refresh)

    def handle_toggle(self, service: str) -> None:
        """Start stopped fixed services; never implement a stop action here."""
        state = self.last_states.get(service, "Unknown")
        if service == "strata":
            self.log("Strata / Qwen toggle is status-only")
            messagebox.showinfo("Strata / Qwen", "Strataの起動・停止・モデル操作はこの画面から行いません。")
            return
        if state in {"Running", "Ready", "Starting"}:
            self.log(f"{self.SERVICE_INFO[service][1]} stop is not implemented; state unchanged")
            messagebox.showinfo("停止不可", "安全な停止機能が未実装のため、稼働状態を維持します。")
            return
        if state == "Stopped" and start_enabled(service, state):
            if service == "hub":
                self.start_hub()
            elif service == "mobile":
                self.start_mobile()
            elif service == "pc":
                self.start_pc()
            return
        self.log(f"{self.SERVICE_INFO.get(service, ('', service))[1]} toggle disabled while state is {state}")
        messagebox.showinfo("操作できません", "状態を安全に確認できないため、この操作は無効です。")

    def show_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Ponkun Home 設定")
        dialog.configure(bg="#17202d")
        dialog.resizable(False, False)
        tk.Label(dialog, text="起動設定", font=("Segoe UI", 14, "bold"), bg="#17202d", fg="#f7f9fc").pack(anchor="w", padx=18, pady=(16, 8))
        auto_open = tk.BooleanVar(value=self.settings.get("auto_start_on_open", True))
        auto_pc = tk.BooleanVar(value=self.settings.get("auto_start_pc", True))
        vision_backend = tk.StringVar(value=str(self.settings.get("vision_backend", "disabled")))
        tk.Checkbutton(dialog, text="Ponkun Homeを開いたときに一括起動", variable=auto_open, bg="#17202d", fg="#f7f9fc", selectcolor="#26354a", activebackground="#17202d", activeforeground="#f7f9fc").pack(anchor="w", padx=18, pady=4)
        tk.Checkbutton(dialog, text="一括起動にPC音声版を含める", variable=auto_pc, bg="#17202d", fg="#f7f9fc", selectcolor="#26354a", activebackground="#17202d", activeforeground="#f7f9fc").pack(anchor="w", padx=18, pady=4)
        tk.Label(dialog, text="画像分析（外部送信）", bg="#17202d", fg="#f7f9fc").pack(anchor="w", padx=18, pady=(10, 2))
        tk.Radiobutton(dialog, text="無効（推奨）", variable=vision_backend, value="disabled", bg="#17202d", fg="#f7f9fc", selectcolor="#26354a", activebackground="#17202d", activeforeground="#f7f9fc").pack(anchor="w", padx=18, pady=2)
        tk.Radiobutton(dialog, text="OpenAI（画像ごとに送信確認）", variable=vision_backend, value="openai", bg="#17202d", fg="#f7f9fc", selectcolor="#26354a", activebackground="#17202d", activeforeground="#f7f9fc").pack(anchor="w", padx=18, pady=2)
        try:
            credential_text = "OpenAI資格情報: " + openai_credential_status()
        except Exception:
            credential_text = "OpenAI資格情報: 確認できません"
        tk.Label(dialog, text=credential_text, bg="#17202d", fg="#aab8ca").pack(anchor="w", padx=18, pady=(4, 0))
        tk.Label(dialog, text="画像分析設定の変更は、次回のMobile起動から反映されます。", bg="#17202d", fg="#f0bd4f").pack(anchor="w", padx=18, pady=(4, 0))
        tk.Label(dialog, text="Strata Desktopとiq3_sのReady確認は手動です。", bg="#17202d", fg="#f0bd4f").pack(anchor="w", padx=18, pady=(8, 12))
        def save_and_close() -> None:
            self.settings = {"auto_start_on_open": auto_open.get(), "auto_start_pc": auto_pc.get(), "vision_backend": vision_backend.get()}
            save_settings(self.settings)
            self.log("起動設定を保存")
            dialog.destroy()
        ttk.Button(dialog, text="保存", command=save_and_close).pack(anchor="e", padx=18, pady=(0, 16))

    def open_strata_key_dialog(self) -> None:
        if self._strata_key_dialog is not None and self._strata_key_dialog.winfo_exists():
            self._strata_key_dialog.lift()
            self._strata_key_dialog.focus_force()
            return
        self._strata_key_dialog = show_strata_key_dialog(self.root)

    def start_hub(self) -> None:
        if not messagebox.askyesno("Hub起動", "登録済みの自動起動タスクを実行しますか？"):
            self.log("Hub起動をキャンセル")
            return
        subprocess.Popen(
            ["schtasks.exe", "/Run", "/TN", HUB_TASK], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=hidden_startupinfo(), creationflags=hidden_creationflags(),
        )
        self.log("Hub起動を要求（状態は再確認後に更新）")
        self.root.after(1500, self.refresh)
        self.root.after(4000, self.refresh)

    def _start_client(self, key: str, path: Path, title: str) -> None:
        if not messagebox.askyesno(f"{title}起動", f"固定ランチャーで{title}を起動しますか？"):
            self.log(f"{title}起動をキャンセル")
            return
        if self.last_states.get(key) == "Running":
            self.log(f"{title}起動を拒否: 既にRunning")
            return
        if not (PYTHONW.is_file() or PYTHON.is_file()):
            self.log(f"{title}起動エラー: Python実行ファイルなし")
            messagebox.showerror("起動失敗", "GUI用に設定されたPython実行ファイルが見つかりません。")
            return
        self._set_starting(key, title)
        if self._launch_background(key) is not None:
            self.log(f"{title}起動を要求（実状態を再確認中）")
        else:
            self.log(f"{title}起動エラー: Python実行に失敗")
            messagebox.showerror("起動失敗", "固定ランチャーを起動できませんでした。詳細はActivity Logを確認してください。")
        self.root.after(1500, self.refresh)
        self.root.after(4000, self.refresh)

    def _set_starting(self, key: str, title: str) -> None:
        self.pending[key] = "Starting"
        self.last_states[key] = "Starting"
        self.card_state[key].configure(text="… STARTING", fg=COLORS["Unknown"])
        self.switches[key].set_state("Starting", False)
        self.card_state[key].configure(text="Starting", fg=COLORS["Unknown"])
        self.start_buttons[key].configure(state="disabled")
        self.log(f"{title} Starting...")

    def start_mobile(self) -> None:
        self._start_client("mobile", MOBILE_LAUNCHER, "Mobile")

    def start_pc(self) -> None:
        self._start_client("pc", PC_LAUNCHER, "PC音声版")

    def copy_tailscale_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(MOBILE_TAILSCALE_URL)
        self.root.update()
        self.log("Galaxy用URLをクリップボードへコピー")


def main() -> int:
    root = tk.Tk()
    ControlCenter(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
