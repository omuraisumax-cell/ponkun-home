"""Local-only Ponkun Hub controller.

The Hub owns only child processes it started itself. Mobile is opt-in real
mode; all other services remain status-only until an explicit adapter exists.
"""
from __future__ import annotations

import json
import hmac
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ponkun_inference import chat as inference_chat
from ponkun_inference import status as inference_status
from ponkun_hub.hub_streaming import HubStreamingError
from ponkun_hub.hub_streaming_endpoint import stream_event_body
from ponkun_hub.secure_credentials import read_strata_key
HOST = os.environ.get("PONKUN_HUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("PONKUN_HUB_PORT", "8787"))
HUB_CLIENT_TOKEN = os.environ.get("PONKUN_HUB_CLIENT_TOKEN", "").strip()
MOBILE_HOST = os.environ.get("PONKUN_MOBILE_HOST", "127.0.0.1")
MOBILE_PORT = int(os.environ.get("PONKUN_MOBILE_PORT", "8002"))
MOBILE_HUB_URL = os.environ.get("PONKUN_MOBILE_HUB_URL", "http://127.0.0.1:8788").strip().rstrip("/")
MOBILE_MODE = os.environ.get("PONKUN_HUB_MOBILE_MODE", "mock").strip().lower()
MOBILE_FILE = Path(os.environ.get("PONKUN_MOBILE_FILE", str(ROOT.parent / "ponkun_mobile.py"))).resolve()
DEFAULT_MOBILE_PYTHON = (ROOT.parent / ".venv" / "Scripts" / "python.exe").resolve()
VERIFIED_MOBILE_PYTHON = DEFAULT_MOBILE_PYTHON
MOBILE_PYTHON = Path(os.environ.get("PONKUN_MOBILE_PYTHON", str(DEFAULT_MOBILE_PYTHON))).resolve()
ALLOW_CUSTOM_PYTHON = os.environ.get("PONKUN_HUB_ALLOW_CUSTOM_PYTHON", "0").strip() == "1"
MOBILE_PYTHONPATH = os.environ.get("PONKUN_MOBILE_PYTHONPATH", "").strip()
MOBILE_LOG = Path(os.environ.get("PONKUN_MOBILE_LOG", str(ROOT / "logs" / "mobile.log"))).resolve()
PC_LAUNCHER = (ROOT.parent / "start_ponkun_pc.cmd").resolve()
TAILSCALE_URL = os.environ.get("PONKUN_TAILSCALE_URL", "").strip().rstrip("/")
MODELS = ("qwen2.5:32b", "qwen2.5:72b")
SERVICES = ("pc", "mobile", "stackchan", "strata", "core")


class MobileProcessManager:
    """Own, log, and inspect only the Mobile child started by this Hub."""

    REQUIRED_MODULES = ("fastapi", "uvicorn", "pydantic", "faster_whisper", "requests", "openai")

    def __init__(self, mode=MOBILE_MODE, host=MOBILE_HOST, port=MOBILE_PORT,
                 mobile_file=MOBILE_FILE, launch_command=None, health_path="/health",
                 required_modules=None, python_executable=MOBILE_PYTHON,
                 log_file=MOBILE_LOG, pythonpath=MOBILE_PYTHONPATH):
        self.mode = mode if mode in ("mock", "real") else "mock"
        self.host = host
        self.port = int(port)
        self.mobile_file = Path(mobile_file).resolve()
        self.launch_command = list(launch_command) if launch_command is not None else None
        self.health_path = health_path
        self.required_modules = tuple(required_modules) if required_modules is not None else self.REQUIRED_MODULES
        self.python_executable = Path(python_executable).resolve()
        self.pythonpath = str(pythonpath or "")
        self.log_file = Path(log_file).resolve()
        self.lock = RLock()
        self.process = None
        self.log_handle = None
        self.started_at = None
        self.last_health_at = None
        self.last_error = None
        self.last_returncode = None

    def _child_env(self):
        env = os.environ.copy()
        # MobileはStrataへ直接接続せず、Bearerキーも子プロセスへ継承しない。
        env.pop("PONKUN_STRATA_API_KEY", None)
        env["PONKUN_MOBILE_HUB_URL"] = MOBILE_HUB_URL
        env["PONKUN_MOBILE_HUB_ONLY"] = "1"
        if self.pythonpath:
            env["PYTHONPATH"] = self.pythonpath
        return env

    def _health(self):
        import urllib.request
        url = f"http://{self.host}:{self.port}{self.health_path}"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                self.last_health_at = time.time()
                return response.status == 200, f"HTTP {response.status}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _readiness(self):
        import urllib.request
        url = f"http://{self.host}:{self.port}/readiness"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}

    def _port_open(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((self.host, self.port)) == 0

    def _python_allowed(self):
        if not self.python_executable.is_file():
            return False
        if self.python_executable == DEFAULT_MOBILE_PYTHON:
            return True
        return ALLOW_CUSTOM_PYTHON

    def build_command(self):
        if self.launch_command is not None:
            return list(self.launch_command)
        return [str(self.python_executable), "-m", "uvicorn", "ponkun_mobile:app",
                "--host", self.host, "--port", str(self.port)]

    def _dependencies(self):
        if not self.required_modules:
            return []
        if not self.python_executable.is_file():
            return [f"python-not-found:{self.python_executable}"]
        script = ("import importlib.util; required=" + repr(list(self.required_modules)) +
                  "; print('\\n'.join(name for name in required if importlib.util.find_spec(name) is None))")
        try:
            result = subprocess.run([str(self.python_executable), "-B", "-c", script],
                                    capture_output=True, text=True, timeout=5,
                                    check=False, env=self._child_env())
        except Exception as exc:
            return [f"python-check-failed:{type(exc).__name__}:{exc}"]
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"returncode={result.returncode}").strip()
            return [f"python-check-failed:{detail[-500:]}"]
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _log_tail(self):
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
            return text[-4000:]
        except OSError:
            return ""

    def snapshot(self):
        with self.lock:
            process = self.process
            if process is not None and process.poll() is not None:
                self.last_returncode = process.returncode
                self.last_error = f"Mobile exited (returncode={process.returncode})"
                self.process = None
                self._close_log()
                process = None
            if self.mode == "mock":
                healthy, health_detail = False, "mock mode"
                status, detail = "mock", "No Mobile process control in mock mode."
            else:
                healthy, health_detail = self._health()
                if not self._python_allowed():
                    status, detail = "configuration-error", f"Python is not allowed or not found: {self.python_executable}"
                elif process is not None:
                    status = "ready" if healthy else "starting"
                    detail = f"Hub-owned PID={process.pid}; {health_detail}"
                elif healthy:
                    status, detail = "external-process", "Port is healthy but process is not owned by this Hub."
                elif self.last_error:
                    status, detail = "error", self.last_error
                elif not self.mobile_file.exists():
                    status, detail = "not-startable", f"Mobile file not found: {self.mobile_file}"
                else:
                    missing = self._dependencies()
                    status = "missing-dependencies" if missing else "stopped"
                    detail = "Missing: " + ", ".join(missing) if missing else "Ready to start."
            return {
                "label": "Ponkun Mobile", "status": status, "detail": detail,
                "mode": self.mode, "host": self.host, "port": self.port,
                "health": healthy, "managed_pid": process.pid if process else None,
                "controllable": self.mode == "real" and process is not None,
                "python_executable": str(self.python_executable),
                "python_allowed": self._python_allowed(),
                "readiness": self._readiness() if healthy else {"ready": False},
                "started_at": self.started_at,
                "last_health_at": self.last_health_at,
                "last_returncode": self.last_returncode,
                "log_file": str(self.log_file),
                "log_tail": self._log_tail() if self.last_error else "",
            }

    def _close_log(self):
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            finally:
                self.log_handle = None

    def start(self):
        with self.lock:
            self.last_error = None
            if self.mode != "real":
                return self.snapshot()
            if not self._python_allowed():
                self.last_error = f"Python is not allowed or not found: {self.python_executable}"
                return self.snapshot()
            if self.process is not None and self.process.poll() is None:
                return self.snapshot()
            healthy, _ = self._health()
            if healthy or self._port_open():
                self.last_error = "Port is already in use; external process was not stopped."
                return self.snapshot()
            missing = self._dependencies()
            if missing:
                self.last_error = "Missing dependencies before start: " + ", ".join(missing)
                return self.snapshot()
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                self.log_handle = self.log_file.open("a", encoding="utf-8", buffering=1)
                self.log_handle.write(f"\n--- Hub start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                self.process = subprocess.Popen(
                    self.build_command(), cwd=str(self.mobile_file.parent), stdin=subprocess.DEVNULL,
                    stdout=self.log_handle, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=self._child_env(),
                )
                self.started_at = time.time()
                self.last_returncode = None
            except Exception as exc:
                self.last_error = f"Start failed: {type(exc).__name__}: {exc}"
                self.process = None
                self._close_log()
                return self.snapshot()
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.process is None or self.process.poll() is not None:
                break
            if self._health()[0]:
                break
            time.sleep(0.2)
        return self.snapshot()

    def stop(self):
        with self.lock:
            if self.process is None:
                return self.snapshot()
            process = self.process
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self.last_returncode = process.returncode
            self.process = None
            self.last_error = None
            self._close_log()
            return self.snapshot()

    def action(self, action):
        if action == "start":
            return self.start()
        if action == "stop":
            return self.stop()
        raise ValueError("unsupported action")


class HubState:
    def __init__(self):
        self.lock = Lock()
        self.model = MODELS[0]
        self.last_action = None
        self.mobile_manager = MobileProcessManager()

    def snapshot(self):
        mobile = self.mobile_manager.snapshot()
        shared = inference_status()
        with self.lock:
            return {
                "mode": "local", "control_mode": mobile["mode"],
                "connected": mobile["health"], "bind_host": HOST,
                "selected_model": self.model, "inference": shared,
                "services": {
                    "pc": {"label": "Ponkun PC", "status": "status-only", "detail": "Not controlled by this Hub."},
                    "mobile": mobile,
                    "stackchan": {"label": "Stack-chan", "status": "not-configured", "detail": "No device gateway configured."},
                    "strata": {"label": "Strata/Qwen", "status": "configured" if shared["configured"] and shared["backend"] == "strata" else "not-configured", "detail": str(shared)},
                    "core": {"label": "Ponkun Core", "status": "status-only", "detail": "Used inside client processes."},
                }, "last_action": self.last_action,
            }

    def choose_model(self, model):
        if model not in MODELS:
            raise ValueError("unsupported model")
        with self.lock:
            self.model = model
        return self.snapshot()

    def mock_action(self, service, action):
        if service not in SERVICES or action not in ("start", "stop"):
            raise ValueError("unsupported service or action")
        with self.lock:
            self.last_action = {"service": service, "action": action, "executed": False,
                                "reason": "Only Mobile real mode is controllable; other services are status-only."}
        return self.snapshot()

    def action(self, service, action):
        if service == "mobile":
            result = self.mobile_manager.action(action)
            with self.lock:
                self.last_action = {"service": service, "action": action,
                                    "executed": result.get("managed_pid") is not None,
                                    "mode": result.get("mode"), "status": result.get("status")}
            return self.snapshot()
        return self.mock_action(service, action)


STATE = HubState()


def _probe_local_http(url: str) -> dict:
    """Probe a loopback health/page endpoint without returning response bodies."""

    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return {"status": "ok", "detail": f"HTTP {response.status}"}
    except urllib.error.HTTPError as exc:
        return {"status": "ng", "detail": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "ng", "detail": type(exc).__name__}


def _safe_dashboard_url(value: str) -> str | None:
    """Allow only local URLs or HTTPS Tailscale URLs for dashboard links."""

    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "https" and parsed.hostname and parsed.hostname.lower().endswith(".ts.net"):
        return value
    if parsed.scheme in ("http", "https") and parsed.hostname in {"127.0.0.1", "localhost"}:
        return value
    return None


def dashboard_snapshot() -> dict:
    """Read-only status data for the local dashboard; never includes secrets."""

    shared = inference_status()
    strata = _probe_local_http("http://127.0.0.1:47831/health")
    mobile = _probe_local_http("http://127.0.0.1:8002/login")
    return {
        "hub": {
            "status": "ok",
            "detail": "Hub API is serving",
            "bind_host": HOST,
            "port": PORT,
            "backend": shared.get("backend"),
            "endpoint": shared.get("endpoint"),
            "model": shared.get("model"),
            "authentication_configured": bool(shared.get("authentication_configured")),
        },
        "strata": {"label": "Strata/Qwen", **strata, "url": "http://127.0.0.1:47831/health"},
        "mobile": {"label": "Ponkun Mobile", **mobile, "url": "http://127.0.0.1:8002/login"},
        "pc": {
            "label": "Ponkun PC",
            "status": "ok" if PC_LAUNCHER.is_file() else "ng",
            "detail": "Launcher is available" if PC_LAUNCHER.is_file() else "Launcher not found",
            "launcher": str(PC_LAUNCHER),
        },
        "tailscale": {
            "status": "ok" if _safe_dashboard_url(TAILSCALE_URL) else "unknown",
            "url": _safe_dashboard_url(TAILSCALE_URL),
            "detail": "Configured HTTPS URL" if _safe_dashboard_url(TAILSCALE_URL) else "Not configured or not verified",
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PonkunHub/1.0"

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/dashboard":
            body = (ROOT / "dashboard.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/dashboard-status":
            self.send_json(HTTPStatus.OK, dashboard_snapshot())
        elif path == "/api/status":
            self.send_json(HTTPStatus.OK, STATE.snapshot())
        elif path == "/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True, "mode": "local", "inference": inference_status()})
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            path = urlsplit(self.path).path
            if path in ("/api/inference", "/api/inference/stream") and HUB_CLIENT_TOKEN:
                supplied = self.headers.get("X-Ponkun-Client-Token", "")
                if not hmac.compare_digest(supplied, HUB_CLIENT_TOKEN):
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Hub client authentication failed"})
                    return
            if path == "/api/inference/stream":
                if os.environ.get("PONKUN_STREAMING_VOICE", "0").strip() != "1":
                    self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "streaming inference is disabled"})
                    return
                if not str(data.get("user_text", "")).strip():
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "user_text is required"})
                    return
                events = iter(stream_event_body(
                    data,
                    client_authorized=True,
                    credential_reader=read_strata_key,
                ))
                try:
                    first_event = next(events)
                except HubStreamingError as exc:
                    status = exc.http_status or HTTPStatus.BAD_GATEWAY
                    self.send_json(status, {"error": exc.code})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(first_event)
                self.wfile.flush()
                try:
                    for event in events:
                        self.wfile.write(event)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionAbortedError):
                    return
                return
            if path == "/api/model":
                result = STATE.choose_model(data.get("model", ""))
            elif path == "/api/action":
                result = STATE.action(data.get("service", ""), data.get("action", ""))
            elif path == "/api/inference":
                instructions = str(data.get("instructions", ""))
                user_text = str(data.get("user_text", ""))
                if not user_text.strip():
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "user_text is required"})
                    return
                result = {"text": inference_chat(STATE.model, instructions, user_text),
                          "inference": inference_status()}
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self.send_json(HTTPStatus.OK, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc), "inference": inference_status()})

    def log_message(self, *_args):
        pass


def make_server(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    server = make_server()
    print(f"Ponkun Hub: http://{HOST}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
