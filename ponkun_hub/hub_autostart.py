"""Safe per-user startup wrapper for the Ponkun Hub.

It never stops or replaces a process. A healthy Hub on the configured port is
treated as already running; any other occupied port is a hard refusal.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .secure_credentials import CredentialError, read_strata_key
except ImportError:
    from ponkun_hub.secure_credentials import CredentialError, read_strata_key

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8788
STRATA_URL = "http://127.0.0.1:47831"
MODEL = "iq3_s"


def _healthy_hub() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/healthz", timeout=1.5) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read().decode("utf-8"))
            inference = data.get("inference", {})
            return bool(data.get("ok")) and inference.get("backend") == "strata"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((HOST, PORT)) == 0


def start_if_needed() -> int:
    if _healthy_hub():
        print("Ponkun Hub is already healthy on 127.0.0.1:8788; no duplicate start.")
        return 0
    if _port_open():
        print("Ponkun Hub startup refused: 127.0.0.1:8788 is occupied by an unverified process.")
        return 2
    try:
        key = read_strata_key()
    except CredentialError:
        print("Ponkun Hub startup refused: Strata credential is missing or invalid.")
        return 3
    if not key:
        print("Ponkun Hub startup refused: Strata credential is not registered.")
        return 3
    env = os.environ.copy()
    env.update({
        "PONKUN_HUB_HOST": HOST,
        "PONKUN_HUB_PORT": str(PORT),
        "PONKUN_INFERENCE_BACKEND": "strata",
        "PONKUN_STRATA_URL": STRATA_URL,
        "PONKUN_STRATA_MODEL": MODEL,
        "PONKUN_STRATA_TIMEOUT": "60",
        "PONKUN_STRATA_MAX_CONCURRENCY": "1",
        "PONKUN_HUB_MOBILE_MODE": "mock",
        "PONKUN_STRATA_API_KEY": key,
    })
    try:
        subprocess.Popen(
            [sys.executable, "-m", "ponkun_hub.server"],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        key = ""
        env.pop("PONKUN_STRATA_API_KEY", None)
    print("Ponkun Hub startup requested on 127.0.0.1:8788.")
    return 0


if __name__ == "__main__":
    raise SystemExit(start_if_needed())
