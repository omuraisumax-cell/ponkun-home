# Ponkun Home

Ponkun Home is a local-first personal assistant stack for Windows. It combines a PC voice client, a mobile web client, a local Hub, local inference, and an optional Fish Audio Cloud proxy.

> Public-release status: **pre-release candidate**. Review the security and license notes before publishing.

## Architecture

```text
Ponkun PC / Ponkun Mobile
          |
          v
   Ponkun Hub (local)
      |          |
      v          v
 local LLM    TTS proxy
               |
               v
        Fish Audio Cloud (optional)
```

The repository contains source code and safe configuration templates only. Credentials, databases, logs, generated audio, model weights, local reports, and user conversations must remain outside version control.

## Requirements

- Windows 10 or 11
- Python 3.12
- Docker Desktop only when using the Fish Audio proxy
- A separately installed local inference runtime/model
- Optional Fish Audio account and API credential

Third-party model weights and service credentials are not distributed with this project.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

   `requirements.txt` pins the direct dependencies tested for this release
   candidate. For an exact reproduction of the audited Windows/Python 3.12
   environment, install `requirements.lock.txt` instead.

3. Copy `.env.example` to a local `.env` only if your launcher loads dotenv files. Otherwise set the same values through Windows Credential Manager or the process environment.
4. Keep all tokens and API keys out of this directory. The Fish proxy expects a file-based Docker secret.
5. Start the Hub on loopback:

   ```powershell
   python -m ponkun_hub.server
   ```

6. Start the PC client with `start_ponkun_pc.cmd`, or run `python ponkun_pc.py` from the activated environment.
7. Optional Control Center settings use `PONKUN_PYTHON` for a custom interpreter and `PONKUN_MOBILE_REMOTE_URL` for a user-supplied remote mobile URL. Leave both empty for local-only use.

## Security model

- Hub and mobile authentication tokens are supplied at runtime.
- Fish Audio credentials are read from a mounted secret file, never committed or embedded in images.
- Network listeners default to loopback where possible.
- Live integrations and paid TTS calls are opt-in; unit tests use mocks.

See `SECURITY.md` before filing issues or publishing a fork.

## Testing

```powershell
python -m unittest discover -v
```

Do not enable variables named `PONKUN_RUN_LIVE_*` during public CI runs.

## Third-party software and models

Ponkun Home integrates with, but does not redistribute, Fish Audio, Whisper, Qwen, and StackChan components. Their licenses and service terms apply independently. See `docs/THIRD_PARTY_NOTICES.md`.

## License

Ponkun Home code and documentation owned by Yuki Kitagawa are licensed under
the MIT License. See `LICENSE` and `docs/LICENSE_SCOPE.md` for the exact scope.
Third-party code, libraries, models, services, and assets retain their own
terms and are not automatically relicensed under Ponkun Home's MIT License.
Branded image and icon files are intentionally omitted because their
provenance and redistribution permission have not been confirmed.

