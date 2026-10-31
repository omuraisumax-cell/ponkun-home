"""Install a Compose secret into private tmpfs, then start the proxy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE = Path("/run/compose-secrets/fish_audio_api_key")
DESTINATION = Path("/run/ponkun-secrets/fish_audio_api_key")
TEMPORARY = Path("/run/ponkun-secrets/.fish_audio_api_key.tmp")


class SecretInitializationError(RuntimeError):
    pass


def _remove_temporary(temporary: Path) -> None:
    try:
        temporary.unlink()
        return
    except FileNotFoundError:
        return
    except OSError:
        pass

    try:
        os.chmod(temporary, 0o600)
        temporary.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecretInitializationError(
            "temporary credential cleanup failed"
        ) from exc


def _set_private_mode(descriptor: int, temporary: Path) -> None:
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(descriptor, 0o400)
    else:
        os.chmod(temporary, 0o400)


def _valid_secret(raw: bytes) -> bool:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return (
        not raw.startswith(b"\xef\xbb\xbf")
        and value.encode("utf-8") == raw
        and 20 <= len(value) <= 512
        and value.isascii()
        and not any(char.isspace() or ord(char) < 33 for char in value)
    )


def install_private_secret(
    source: Path = SOURCE,
    destination: Path = DESTINATION,
    temporary: Path = TEMPORARY,
) -> None:
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SecretInitializationError("credential source unavailable") from exc
    if not _valid_secret(raw):
        raise SecretInitializationError("credential source invalid")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    temporary_created = False
    try:
        descriptor = os.open(temporary, flags, 0o400)
        temporary_created = True
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("credential write did not progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        _set_private_mode(descriptor, temporary)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
    except OSError as exc:
        raise SecretInitializationError("private credential installation failed") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raw = b""
        if temporary_created:
            _remove_temporary(temporary)


def main() -> int:
    try:
        install_private_secret()
    except SecretInitializationError:
        print("Fish Audio credential initialization failed", file=sys.stderr)
        return 1
    os.execvp(
        "uvicorn",
        ["uvicorn", "tts_proxy:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"],
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
