"""Tkinter dialog helpers for safe Strata credential registration.

The dialog does not perform any action until the user presses Save or Test.
Secrets are held only in widget memory and are cleared after saving.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

from .secure_credentials import CredentialError, credential_status, read_strata_key, write_strata_key
from .strata_auth_diagnostic import run_once


def show_strata_key_dialog(parent: tk.Misc) -> tk.Toplevel:
    window = tk.Toplevel(parent)
    window.title("Strata APIキー設定")
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()

    tk.Label(window, text="Strata APIキー（画面・ログ・ファイルには保存しません）").pack(padx=16, pady=(14, 4))
    entry = tk.Entry(window, width=58, show="*")
    entry.pack(padx=16, pady=4)
    entry.focus_set()
    status = tk.StringVar(value="保存済み状態を確認中…")
    tk.Label(window, textvariable=status, anchor="w").pack(fill="x", padx=16, pady=4)

    def refresh_status() -> None:
        try:
            status.set("保存状態: " + credential_status())
        except Exception:
            status.set("保存状態: 確認できません")

    def save() -> None:
        value = entry.get()
        try:
            write_strata_key(value)
        except CredentialError as exc:
            messagebox.showerror("保存失敗", str(exc), parent=window)
            return
        finally:
            entry.delete(0, tk.END)
        status.set("保存状態: present-valid")
        messagebox.showinfo("保存完了", "Credential Managerへ保存しました。Hubの再起動が必要です。", parent=window)

    def test_connection() -> None:
        status.set("接続テスト中…")

        def worker() -> None:
            result = run_once()

            def update() -> None:
                status.set(f"接続: {result.http_status} / {result.authentication} / iq3_s={result.target_model}")

            window.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    buttons = tk.Frame(window)
    buttons.pack(pady=(8, 14))
    tk.Button(buttons, text="保存", width=12, command=save).pack(side="left", padx=4)
    tk.Button(buttons, text="接続テスト", width=12, command=test_connection).pack(side="left", padx=4)
    tk.Button(buttons, text="キャンセル", width=12, command=window.destroy).pack(side="left", padx=4)
    refresh_status()
    return window
