"""Windows Credential Manager access for Ponkun Hub secrets.

The module deliberately exposes only presence/validity to callers that do not
need the secret. The Strata key is kept in memory only while a child process
is being started or an HTTP request is being made.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import secrets
import sys
from ctypes import wintypes

TARGET_NAME = "PonkunHome/StrataApiKey"
MOBILE_TARGET_NAME = "PonkunHome/MobileAuthToken"
OPENAI_TARGET_NAME = "PonkunHome/OpenAIApiKey"
HUB_CLIENT_TARGET_NAME = "PonkunHome/HubClientToken"
ERROR_NOT_FOUND = 1168
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class CredentialError(RuntimeError):
    """A credential is unavailable or unsafe for an HTTP header."""


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", wintypes.LPVOID), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is unavailable")
    return ctypes.WinDLL("advapi32", use_last_error=True)


def _validate_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialError("Strata credential is missing")
    if value != value.strip():
        raise CredentialError("Strata credential has leading or trailing whitespace")
    if any(ord(ch) > 255 for ch in value):
        raise CredentialError("Strata credential contains non-ASCII characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise CredentialError("Strata credential contains control characters")
    return value


def _write_credential_blob(value: str, target_name: str = TARGET_NAME) -> None:
    value = _validate_key(value)
    api = _advapi32()
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    blob = value.encode("utf-8")
    blob_buffer = ctypes.create_string_buffer(blob)
    credential = _Credential(
        0, CRED_TYPE_GENERIC, target_name, None, wintypes.FILETIME(),
        len(blob), ctypes.cast(blob_buffer, wintypes.LPVOID),
        CRED_PERSIST_LOCAL_MACHINE, 0, None, None, "PonkunHub",
    )
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError("Windows Credential Manager write failed")


def _read_credential_blob(target_name: str = TARGET_NAME) -> str | None:
    api = _advapi32()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.c_void_p)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    raw = ctypes.c_void_p()
    if not api.CredReadW(target_name, CRED_TYPE_GENERIC, 0, ctypes.byref(raw)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise CredentialError("Windows Credential Manager read failed")
    try:
        credential = ctypes.cast(raw, ctypes.POINTER(_Credential)).contents
        data = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialError("Stored Strata credential is not valid UTF-8") from exc
    finally:
        api.CredFree(raw)


def write_strata_key(value: str) -> None:
    """Validate and save a key without printing or returning it."""
    _write_credential_blob(value)


def read_strata_key() -> str | None:
    value = _read_credential_blob()
    if value is None:
        return None
    return _validate_key(value)


def write_mobile_token(value: str) -> None:
    """Validate and save the separate Mobile login secret."""
    _validate_mobile_token(value)
    _write_credential_blob(value, MOBILE_TARGET_NAME)


def read_mobile_token() -> str | None:
    value = _read_credential_blob(MOBILE_TARGET_NAME)
    if value is None:
        return None
    return _validate_mobile_token(value)


def _validate_openai_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialError("OpenAI credential is missing")
    if value != value.strip():
        raise CredentialError("OpenAI credential has leading or trailing whitespace")
    if any(ord(ch) > 255 or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise CredentialError("OpenAI credential contains unsupported characters")
    return value


def write_openai_key(value: str) -> None:
    _write_credential_blob(_validate_openai_key(value), OPENAI_TARGET_NAME)


def read_openai_key() -> str | None:
    value = _read_credential_blob(OPENAI_TARGET_NAME)
    return _validate_openai_key(value) if value is not None else None


def _validate_hub_client_token(value: str) -> str:
    """Validate a Hub client token without exposing any part of it."""
    if not isinstance(value, str) or not value:
        raise CredentialError("Hub client credential is missing")
    if value != value.strip():
        raise CredentialError("Hub client credential has leading or trailing whitespace")
    if any(ord(ch) > 255 or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise CredentialError("Hub client credential contains unsupported characters")
    return value


def write_hub_client_token(value: str, writer=None) -> None:
    """Save the dedicated Hub client token without printing or returning it.

    ``writer`` is injectable so tests never access the real Credential Manager.
    Registration and token generation remain separate, explicitly approved steps.
    """
    value = _validate_hub_client_token(value)
    sink = writer or _write_credential_blob
    sink(value, HUB_CLIENT_TARGET_NAME)


def read_hub_client_token(reader=None, *, required: bool = False) -> str | None:
    """Read the dedicated Hub client token through an injectable reader.

    A missing optional credential returns ``None``. Required mode fails closed.
    Other Credential Manager errors continue to raise ``CredentialError``.
    """
    source = reader or _read_credential_blob
    value = source(HUB_CLIENT_TARGET_NAME)
    if value is None:
        if required:
            raise CredentialError("Hub client credential is required")
        return None
    return _validate_hub_client_token(value)


def hub_client_credential_status(reader=None) -> str:
    """Return only a non-secret presence/validity classification."""
    try:
        value = read_hub_client_token(reader=reader)
        return "present-valid" if value else "missing"
    except CredentialError:
        return "present-invalid"


def openai_credential_status() -> str:
    try:
        value = read_openai_key()
        return "present-valid" if value else "missing"
    except CredentialError:
        return "present-invalid"


def register_openai_gui() -> int:
    return register_gui(OPENAI_TARGET_NAME, "Ponkun Home - OpenAI web-search credential registration")


def mobile_credential_status() -> str:
    try:
        value = read_mobile_token()
        return "present-valid" if value else "missing"
    except CredentialError:
        return "present-invalid"


def credential_status() -> str:
    try:
        value = read_strata_key()
        return "present-valid" if value else "missing"
    except CredentialError:
        return "present-invalid"


def register_gui(target_name: str = TARGET_NAME, title: str = "Ponkun Hub - credential registration") -> int:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(title)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    frame = tk.Frame(root, padx=16, pady=14)
    frame.pack()
    label = "Enter the OpenAI API key. It will not be displayed or saved in a file." if target_name == OPENAI_TARGET_NAME else "Enter the Strata API key. It will not be displayed or saved in a file."
    tk.Label(frame, text=label).pack()
    entry = tk.Entry(frame, width=64)
    entry.pack(pady=(10, 8))
    entry.focus_set()
    result = {"ok": False}

    def submit() -> None:
        try:
            if target_name == MOBILE_TARGET_NAME:
                write_mobile_token(entry.get())
            else:
                _write_credential_blob(entry.get(), target_name)
        except CredentialError as exc:
            messagebox.showerror("Registration failed", str(exc), parent=root)
            return
        result["ok"] = True
        entry.delete(0, tk.END)
        messagebox.showinfo("Registration complete", "The credential was stored in Windows Credential Manager.", parent=root)
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.pack()
    tk.Button(buttons, text="Save", width=10, command=submit).pack(side=tk.LEFT, padx=4)
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side=tk.LEFT, padx=4)
    root.bind("<Return>", lambda _event: submit())
    root.mainloop()
    return 0 if result["ok"] else 1


def _validate_mobile_token(value: str) -> str:
    value = _validate_key(value)
    if len(value) < 10:
        raise CredentialError("Mobile token must be at least 10 characters")
    lowered = value.lower()
    weak_values = {"password123", "password1234", "ponkun1234", "qwerty1234", "1234567890"}
    if lowered in weak_values or len(set(value)) <= 3:
        raise CredentialError("Mobile token is too weak")
    classes = sum([
        bool(any(ch.islower() for ch in value)),
        bool(any(ch.isupper() for ch in value)),
        bool(any(ch.isdigit() for ch in value)),
        bool(any(not ch.isalnum() for ch in value)),
    ])
    if classes < 2:
        raise CredentialError("Mobile token must use at least two character classes")
    return value


def register_generated_mobile_gui() -> int:
    """Offer manual or generated Mobile-token registration in a GUI only."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Ponkun Mobile - login credential registration")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    frame = tk.Frame(root, padx=16, pady=14)
    frame.pack()
    tk.Label(frame, text="Choose a Mobile login credential. It is not saved to a file or log.").pack()
    mode = tk.StringVar(value="manual")
    show_secret = tk.BooleanVar(value=False)
    generated = {"value": secrets.token_urlsafe(32)}
    result = {"ok": False}

    choices = tk.Frame(frame)
    choices.pack(anchor="w", pady=(8, 4))
    tk.Radiobutton(choices, text="自分でパスワードを設定", variable=mode, value="manual").pack(side=tk.LEFT)
    tk.Radiobutton(choices, text="安全なランダムトークンを生成", variable=mode, value="random").pack(side=tk.LEFT)

    manual_frame = tk.Frame(frame)
    tk.Label(manual_frame, text="パスワード（10文字以上推奨）").grid(row=0, column=0, sticky="w")
    password = tk.Entry(manual_frame, width=42, show="*")
    password.grid(row=1, column=0, pady=(2, 6))
    tk.Label(manual_frame, text="確認用パスワード").grid(row=2, column=0, sticky="w")
    confirmation = tk.Entry(manual_frame, width=42, show="*")
    confirmation.grid(row=3, column=0, pady=(2, 4))
    manual_frame.pack(anchor="w")

    random_frame = tk.Frame(frame)
    random_entry = tk.Entry(random_frame, width=58)
    random_entry.insert(0, generated["value"])
    random_entry.configure(state="readonly")
    random_entry.pack(side=tk.LEFT)

    def apply_visibility() -> None:
        mask = "" if show_secret.get() else "*"
        password.configure(show=mask)
        confirmation.configure(show=mask)
        random_entry.configure(state="normal")
        random_entry.configure(show=mask)
        random_entry.configure(state="readonly")

    def copy_random() -> None:
        root.clipboard_clear()
        root.clipboard_append(generated["value"])
        root.update()

    tk.Button(random_frame, text="コピー", command=copy_random).pack(side=tk.LEFT, padx=(6, 0))
    tk.Label(random_frame, text="表示中の値を手動で管理してください").pack(side=tk.LEFT, padx=(6, 0))

    def switch_mode(*_args) -> None:
        if mode.get() == "random":
            manual_frame.pack_forget()
            random_frame.pack(anchor="w", pady=(4, 0))
        else:
            random_frame.pack_forget()
            manual_frame.pack(anchor="w")

    mode.trace_add("write", switch_mode)
    tk.Checkbutton(frame, text="秘密情報を表示", variable=show_secret, command=apply_visibility).pack(anchor="w", pady=(6, 0))
    status = tk.Label(frame, text="未登録")
    status.pack(anchor="w", pady=(4, 0))

    def submit() -> None:
        token = generated["value"] if mode.get() == "random" else password.get()
        if mode.get() == "manual" and token != confirmation.get():
            messagebox.showerror("Registration failed", "パスワードと確認用パスワードが一致しません。", parent=root)
            return
        try:
            write_mobile_token(token)
        except CredentialError as exc:
            messagebox.showerror("Registration failed", str(exc), parent=root)
            return
        result["ok"] = True
        password.delete(0, tk.END)
        confirmation.delete(0, tk.END)
        random_entry.configure(state="normal")
        random_entry.delete(0, tk.END)
        random_entry.configure(state="readonly")
        generated["value"] = ""
        status.configure(text="present-valid")
        messagebox.showinfo("Registration complete", "Mobile credential stored. Secret fields were cleared.", parent=root)
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.pack(pady=(8, 0))
    tk.Button(buttons, text="Save", width=10, command=submit).pack(side=tk.LEFT, padx=4)
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side=tk.LEFT, padx=4)
    root.bind("<Return>", lambda _event: submit())
    root.mainloop()
    generated["value"] = ""
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--register-mobile", action="store_true")
    parser.add_argument("--register-mobile-generated", action="store_true")
    parser.add_argument("--register-openai", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--status-mobile", action="store_true")
    parser.add_argument("--status-openai", action="store_true")
    args = parser.parse_args(argv)
    if args.register:
        return register_gui(TARGET_NAME, "Ponkun Hub - Strata credential registration")
    if args.register_mobile:
        return register_gui(MOBILE_TARGET_NAME, "Ponkun Mobile - login credential registration")
    if args.register_mobile_generated:
        return register_generated_mobile_gui()
    if args.register_openai:
        return register_openai_gui()
    if args.status_mobile:
        print(mobile_credential_status())
    if args.status_openai:
        print(openai_credential_status())
        return 0
    if args.status or not args.register:
        print(credential_status())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
