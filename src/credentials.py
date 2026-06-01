"""Per-platform login credentials, stored in the OS keychain.

Funded-account secrets must never sit in a plaintext file or env var, so
they live in the operating-system keychain (macOS Keychain / Windows
Credential Locker / Linux Secret Service) via `keyring`. Nothing here
writes a secret to disk, logs it, or commits it.

Set credentials locally — input is not echoed or logged, and never passes
through anything but your own terminal:

    uv run python -m src.credentials set betano
    uv run python -m src.credentials status
    uv run python -m src.credentials delete betano

Read them in code:

    from src.credentials import get_credential
    cred = get_credential("betano")
    if cred is None:
        ...  # not configured

Note: logged-in *recon* needs no credentials here — log in by hand once
via `recon.py --login` and the persistent browser profile holds the
session. These stored credentials are for automated re-login / bet
execution, where a saved browser session isn't enough.
"""

from __future__ import annotations

import contextlib
import getpass
import sys
from dataclasses import dataclass, field

import keyring
import keyring.errors
import structlog

log = structlog.get_logger(__name__)

# Keychain "service" namespace; each entry is stored under this service
# with a "<platform>:username" / "<platform>:password" account key.
SERVICE = "arby"
PLATFORMS: tuple[str, ...] = ("betano", "betsson", "betwarrior", "bplay")


@dataclass(frozen=True)
class Credential:
    """A platform login. `password` is excluded from `repr` so it can't
    leak into logs or tracebacks."""

    platform: str
    username: str
    password: str = field(repr=False)


def _username_key(platform: str) -> str:
    return f"{platform}:username"


def _password_key(platform: str) -> str:
    return f"{platform}:password"


def get_credential(platform: str) -> Credential | None:
    """Return the stored credential for `platform`, or None if unset."""
    username = keyring.get_password(SERVICE, _username_key(platform))
    password = keyring.get_password(SERVICE, _password_key(platform))
    if username is None or password is None:
        return None
    return Credential(platform=platform, username=username, password=password)


def set_credential(platform: str, username: str, password: str) -> None:
    """Store/replace the credential for `platform` in the OS keychain."""
    keyring.set_password(SERVICE, _username_key(platform), username)
    keyring.set_password(SERVICE, _password_key(platform), password)
    log.info("credential.set", platform=platform)  # platform only — never the secret


def delete_credential(platform: str) -> None:
    """Remove a platform's credential from the keychain (no-op if absent)."""
    for key in (_username_key(platform), _password_key(platform)):
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, key)
    log.info("credential.deleted", platform=platform)


def _cli(argv: list[str]) -> int:
    """Tiny local CLI. Secrets are entered via getpass (not echoed/logged)."""
    if not argv or argv[0] not in ("set", "status", "delete"):
        print(
            "usage: python -m src.credentials {set|status|delete} [platform]",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]

    if cmd == "status":
        for platform in PLATFORMS:
            state = "configured" if get_credential(platform) else "-"
            print(f"  {platform:11} {state}")
        return 0

    if len(argv) < 2:
        print(f"error: '{cmd}' needs a platform ({', '.join(PLATFORMS)})", file=sys.stderr)
        return 2
    platform = argv[1]
    if platform not in PLATFORMS:
        print(f"error: unknown platform {platform!r}; expected {PLATFORMS}", file=sys.stderr)
        return 2

    if cmd == "delete":
        delete_credential(platform)
        print(f"deleted {platform}")
        return 0

    username = input(f"{platform} username: ").strip()
    password = getpass.getpass(f"{platform} password (hidden): ")
    if not username or not password:
        print("error: username and password are both required", file=sys.stderr)
        return 2
    set_credential(platform, username, password)
    print(f"stored {platform} in the OS keychain")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
