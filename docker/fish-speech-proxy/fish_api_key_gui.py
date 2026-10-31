"""Windows GUI for managing the Docker-only Fish Audio credential.

The secret is stored once, outside the repository, for Docker Compose to mount.
No credential value, fragment, or length is displayed or logged.
"""
from __future__ import annotations

import getpass
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


STATUS_MISSING = "missing"
STATUS_VALID = "present-valid"
STATUS_INVALID = "present-invalid"
SECRET_NAME = "fish_audio_api_key"


def default_secret_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA", "").strip()
    if not base:
        raise RuntimeError("Local application data directory is unavailable")
    return Path(base) / "PonkunHome" / "DockerSecrets" / SECRET_NAME


def locally_valid(value: str) -> bool:
    return (
        20 <= len(value) <= 512
        and value.isascii()
        and not any(char.isspace() or ord(char) < 33 for char in value)
    )


def credential_status(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return STATUS_MISSING
    except OSError:
        return STATUS_INVALID
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return STATUS_INVALID
    if raw.startswith(b"\xef\xbb\xbf") or value.encode("utf-8") != raw:
        return STATUS_INVALID
    return STATUS_VALID if locally_valid(value) else STATUS_INVALID


def _run_acl(args: list[str]) -> None:
    completed = subprocess.run(
        ["icacls", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("Access control could not be secured")


def secure_acl(path: Path, *, is_directory: bool = False) -> None:
    user = getpass.getuser()
    rights = "(OI)(CI)F" if is_directory else "F"
    _run_acl([
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{user}:{rights}",
        f"*S-1-5-18:{rights}",
    ])


def save_secret(
    value: str,
    path: Path,
    *,
    acl_setter: Callable[..., None] = secure_acl,
) -> None:
    if not locally_valid(value):
        raise ValueError("Credential format is invalid")
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        acl_setter(directory, is_directory=True)
    except Exception:
        if not any(directory.iterdir()):
            directory.rmdir()
        raise RuntimeError("Secure storage could not be prepared") from None

    fd = -1
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".fish-secret-", dir=directory)
        temporary = Path(name)
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        acl_setter(temporary, is_directory=False)
        os.replace(temporary, path)
        temporary = None
    except Exception:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError("Credential was not saved") from None


def create_gui(*, path: Path | None = None, store: Callable[[str, Path], None] = save_secret):
    import tkinter as tk
    from tkinter import messagebox, ttk

    secret_path = path or default_secret_path()
    root = tk.Tk()
    root.title("Ponkun Fish Audio credential")
    root.resizable(False, False)
    value = tk.StringVar()
    status = tk.StringVar(value=credential_status(secret_path))

    ttk.Label(root, text="Fish Audio API key").grid(row=0, column=0, padx=12, pady=12)
    entry = ttk.Entry(root, textvariable=value, show="•", width=48)
    entry.grid(row=0, column=1, padx=12, pady=12)
    ttk.Label(root, textvariable=status).grid(row=1, column=0, columnspan=2, pady=6)

    def refresh() -> None:
        status.set(credential_status(secret_path))

    def register() -> None:
        candidate = value.get()
        if not locally_valid(candidate):
            status.set(STATUS_INVALID)
            messagebox.showerror("Registration failed", "Credential format is invalid.")
            return
        if not messagebox.askyesno("Confirm", "Register or replace the Docker credential?"):
            return
        try:
            store(candidate, secret_path)
        except Exception:
            status.set(STATUS_INVALID)
            messagebox.showerror("Registration failed", "Secure storage could not be updated.")
        else:
            value.set("")
            refresh()
            messagebox.showinfo("Registered", "Credential status: present-valid")

    ttk.Button(root, text="Register / Update", command=register).grid(row=2, column=0, padx=12, pady=12)
    ttk.Button(root, text="Check status", command=refresh).grid(row=2, column=1, padx=12, pady=12)
    entry.focus_set()
    return root


def main() -> None:
    create_gui().mainloop()


if __name__ == "__main__":
    main()
