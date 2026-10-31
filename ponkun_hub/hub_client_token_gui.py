"""Dedicated, non-secret GUI for registering the Hub client token.

Nothing is generated or written by importing this module.  A token is generated
only after the user confirms Save, is passed directly to Credential Manager, and
is then discarded.  The GUI exposes only the credential status classification.
"""
from __future__ import annotations

import secrets

from ponkun_hub.secure_credentials import (
    CredentialError,
    hub_client_credential_status,
    write_hub_client_token,
)


def generate_hub_client_token(*, generator=secrets.token_urlsafe) -> str:
    """Generate at least 384 bits of entropy without displaying the value."""
    return generator(48)


def register_generated_hub_client_token(*, generator=secrets.token_urlsafe, writer=None) -> str:
    """Generate and store a token, returning only a non-secret status."""
    token = generate_hub_client_token(generator=generator)
    try:
        write_hub_client_token(token, writer=writer)
    finally:
        token = ""
    return "present-valid"


def run_gui(*, register=register_generated_hub_client_token,
            status_reader=hub_client_credential_status) -> int:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Ponkun Home - Hub client credential")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = tk.Frame(root, padx=18, pady=16)
    frame.pack()
    tk.Label(
        frame,
        text=("Hub専用トークンをWindows Credential Managerへ登録します。\n"
              "値は画面、ログ、ファイル、コマンドラインへ表示しません。"),
        justify="left",
    ).pack(anchor="w")
    status = tk.StringVar(value=status_reader())
    tk.Label(frame, textvariable=status).pack(anchor="w", pady=(10, 8))
    confirmed = tk.BooleanVar(value=False)
    tk.Checkbutton(frame, text="専用トークンを生成して登録する", variable=confirmed).pack(anchor="w")
    result = {"ok": False}

    def save():
        if not confirmed.get():
            messagebox.showwarning("Confirmation required", "確認欄を選択してください。", parent=root)
            return
        try:
            register()
            status.set(status_reader())
        except CredentialError:
            status.set("present-invalid")
            messagebox.showerror("Registration failed", "Credential Managerへの登録に失敗しました。", parent=root)
            return
        if status.get() != "present-valid":
            messagebox.showerror("Verification failed", "登録後の状態確認に失敗しました。", parent=root)
            return
        result["ok"] = True
        messagebox.showinfo("Registration complete", "present-valid", parent=root)
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.pack(pady=(10, 0))
    tk.Button(buttons, text="Save", width=12, command=save).pack(side=tk.LEFT, padx=4)
    tk.Button(buttons, text="Cancel", width=12, command=root.destroy).pack(side=tk.LEFT, padx=4)
    root.mainloop()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(run_gui())
