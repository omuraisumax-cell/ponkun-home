"""One-shot direct Strata check and isolated Hub smoke test.

The API key is accepted only from the environment or a masked one-shot GUI,
held in memory, and passed only to the child Hub process.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("PONKUN_STRATA_URL", "http://127.0.0.1:47831").rstrip("/")
MODEL = os.environ.get("PONKUN_STRATA_MODEL", "iq3_s")
API_KEY = os.environ.get("PONKUN_STRATA_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("PONKUN_STRATA_TIMEOUT", "60"))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_api_key() -> str:
    """Read the key once without putting it in command history or files."""
    if API_KEY:
        return API_KEY
    force_gui = os.environ.get("PONKUN_STRATA_GUI_INPUT", "").strip() == "1"
    try:
        if sys.stdin.isatty() and not force_gui:
            try:
                return getpass.getpass("Strata API key (input hidden): ").strip()
            except (EOFError, OSError):
                return ""
    except (AttributeError, OSError):
        pass

    try:
        import tkinter as tk

        result = {"value": "", "cancelled": False}
        root = tk.Tk()
        root.title("Ponkun Home - Strata API key")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        tk.Label(
            root,
            text=(
                "Strata APIキーを一度だけ入力してください。\n"
                "値は保存・表示されません。"
            ),
        ).pack(padx=20, pady=(18, 8))
        entry = tk.Entry(root, show="*", width=48)
        entry.pack(padx=20, pady=8)
        entry.focus_set()

        def submit(*_args):
            result["value"] = entry.get().strip()
            entry.delete(0, tk.END)
            root.destroy()

        def cancel(*_args):
            result["cancelled"] = True
            entry.delete(0, tk.END)
            root.destroy()

        buttons = tk.Frame(root)
        buttons.pack(pady=(4, 18))
        tk.Button(buttons, text="送信", command=submit, width=10).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(buttons, text="キャンセル", command=cancel, width=10).pack(
            side=tk.LEFT, padx=5
        )
        root.bind("<Return>", submit)
        root.bind("<Escape>", cancel)
        root.protocol("WM_DELETE_WINDOW", cancel)
        root.mainloop()
        return "" if result["cancelled"] else result["value"]
    except Exception as exc:
        print(f"API key input is unavailable ({type(exc).__name__}); no authenticated request sent")
        return ""


def request_json(path: str, payload: dict | None = None, base_url: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    started = time.perf_counter()
    request = urllib.request.Request(
        (base_url or BASE_URL) + path,
        data=data,
        headers=headers,
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            elapsed = round((time.perf_counter() - started) * 1000)
            try:
                parsed = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
            return response.status, elapsed, parsed
    except urllib.error.HTTPError as exc:
        return exc.code, round((time.perf_counter() - started) * 1000), None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"request failed: {type(exc).__name__}") from exc


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_pc_client_smoke(hub_url: str) -> int:
    """Exercise the real PC text path without touching the persistent DB."""
    db_path = os.path.join(tempfile.gettempdir(), f"ponkun_phase2_pc_{os.getpid()}.db")
    child_env = os.environ.copy()
    child_env.update({"PONKUN_HUB_URL": hub_url, "PONKUN_MEMORY_DB": db_path})
    code = (
        "from ponkun_core import ask_ponkun; "
        "r=ask_ponkun('こんにちは。名前と役割を一文で説明してください。', "
        "session_id='phase2-pc-smoke'); "
        "a=r['answer']; print('route='+r['route']); "
        "print('nonempty='+str(bool(a.strip()))); "
        "print('has_japanese='+str(any('\\u3040' <= c <= '\\u9fff' for c in a)))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=75,
            check=False,
        )
        if result.returncode != 0:
            print("pc_client: failed")
            return 1
        output = result.stdout.strip()
        nonempty = "nonempty=True" in output
        japanese = "has_japanese=True" in output
        print(f"pc_client: nonempty={nonempty}, has_japanese={japanese}")
        return 0 if nonempty and japanese else 1
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


def run_mobile_client_smoke(hub_url: str) -> int:
    """Exercise Mobile /health and /chat without loading Whisper or using audio."""
    port = _free_local_port()
    db_path = os.path.join(tempfile.gettempdir(), f"ponkun_phase2_mobile_{os.getpid()}.db")
    child_env = os.environ.copy()
    child_env.update({"PONKUN_HUB_URL": hub_url, "PONKUN_MEMORY_DB": db_path})
    child = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ponkun_mobile:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    mobile_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        health = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                print("mobile_client: child exited before health check")
                return 1
            try:
                health = request_json("/health", base_url=mobile_url)
                break
            except RuntimeError:
                time.sleep(0.2)
        if health is None or health[0] != 200:
            print("mobile_client: health check failed")
            return 1
        payload = {"text": "こんにちは。名前と役割を一文で説明してください。", "session_id": "phase2-mobile-smoke"}
        status, _elapsed, result = request_json("/chat", payload, base_url=mobile_url)
        if status != 200 or not isinstance(result, dict):
            print(f"mobile_client: chat HTTP {status}")
            return 1
        answer = result.get("display_text")
        nonempty = isinstance(answer, str) and bool(answer.strip())
        japanese = nonempty and any("\u3040" <= char <= "\u9fff" for char in answer)
        print(f"mobile_client: nonempty={nonempty}, has_japanese={japanese}")
        return 0 if nonempty and japanese else 1
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        try:
            os.remove(db_path)
        except OSError:
            pass


def run_temporary_hub() -> int:
    """Start, check, exercise, and stop only the Hub child created here."""
    port = _free_local_port()
    hub_url = f"http://127.0.0.1:{port}"
    child_env = os.environ.copy()
    child_env.update(
        {
            "PONKUN_HUB_HOST": "127.0.0.1",
            "PONKUN_HUB_PORT": str(port),
            "PONKUN_INFERENCE_BACKEND": "strata",
            "PONKUN_STRATA_URL": BASE_URL,
            "PONKUN_STRATA_MODEL": MODEL,
            "PONKUN_STRATA_API_KEY": API_KEY,
            "PONKUN_STRATA_TIMEOUT": str(TIMEOUT),
        }
    )
    child = subprocess.Popen(
        [sys.executable, "-m", "ponkun_hub.server"],
        cwd=PROJECT_ROOT,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        health = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                print("temporary_hub: child exited before health check")
                return 1
            try:
                health = request_json("/healthz", base_url=hub_url)
                break
            except RuntimeError:
                time.sleep(0.2)
        if health is None:
            print("temporary_hub: health check timed out")
            return 1
        print(f"temporary_hub: health HTTP {health[0]}, {health[1]} ms")
        if health[0] != 200:
            return 1
        payload = {
            "model": MODEL,
            "instructions": "日本語で短く答えてください。",
            "user_text": "こんにちは。名前と役割を一文で説明してください。",
        }
        status, elapsed, result = request_json(
            "/api/inference", payload, base_url=hub_url
        )
        print(f"temporary_hub: inference HTTP {status}, {elapsed} ms")
        if status != 200 or not isinstance(result, dict):
            return 1
        answer = result.get("answer") or result.get("text") or result.get("content")
        print(f"temporary_hub: answer {answer.strip() if isinstance(answer, str) else '(empty)'}")
        if not isinstance(answer, str) or not answer.strip():
            return 1
        if run_pc_client_smoke(hub_url) != 0:
            return 1
        return run_mobile_client_smoke(hub_url)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main() -> int:
    global API_KEY
    status, elapsed, _health = request_json("/health")
    print(f"health: HTTP {status}, {elapsed} ms")
    if status != 200:
        print("health: Strata is not ready; no authenticated request sent")
        return 2

    if not API_KEY:
        API_KEY = read_api_key()
    if not API_KEY:
        print("API key was empty; no authenticated request sent")
        return 2

    status, elapsed, models = request_json("/v1/models")
    print(f"models: HTTP {status}, {elapsed} ms")
    if status != 200 or not isinstance(models, dict):
        API_KEY = ""
        return 3

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "日本語で短く答えてください。"},
            {"role": "user", "content": "こんにちは。名前と役割を一文で説明してください。"},
        ],
        "stream": False,
    }
    status, elapsed, result = request_json("/v1/chat/completions", payload)
    print(f"chat: HTTP {status}, {elapsed} ms")
    if status != 200 or not isinstance(result, dict):
        API_KEY = ""
        return 4
    try:
        answer = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("chat: response shape is not OpenAI-compatible")
        API_KEY = ""
        return 5
    print(f"model: {MODEL}")
    if not isinstance(answer, str) or not answer.strip():
        print("chat: response content was empty")
        API_KEY = ""
        return 5
    print(f"answer: {answer.strip()}")
    hub_status = run_temporary_hub()
    API_KEY = ""
    return hub_status


if __name__ == "__main__":
    raise SystemExit(main())
