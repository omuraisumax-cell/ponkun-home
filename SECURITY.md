# Security Policy

## Reporting a vulnerability

Do not include credentials, private logs, conversations, audio, database contents, or personal network addresses in a public issue. Contact the maintainer privately and provide only the minimum reproduction data.

## Credential handling

- Never commit API keys, tokens, passwords, private keys, or credential export files.
- Revoked credentials remain sensitive and must be removed from the publishable snapshot and reviewed in every Git history that contained them.
- Use Windows Credential Manager or an equivalent secret store for local processes.
- Use file-based Docker secrets for the Fish Audio proxy. Do not pass secrets in image layers, environment variables, command arguments, or logs.

## Before every public release

1. Run the public-release audit described in `docs/PUBLIC_RELEASE_AUDIT.md`.
2. Review tracked files, staged changes, and the complete Git history.
3. Rotate any credential that may have entered a working tree, backup, log, report, or history.
4. Confirm that public tests do not make live or paid network requests.

