"""Secret references → values, in memory only.

``sim:``      the stand-in app's published synthetic demo credentials (a deliberate mock of a vault)
``env:``      an environment variable
``keychain:`` a macOS Keychain generic password, by service name
``vault:``    the production seam; not implemented here

Values are never logged; callers register them with the redactor as ``secret``.
"""

from __future__ import annotations

import os
import shutil
import subprocess


class SecretUnavailable(RuntimeError):
    """Raised without the value or any command output in the message."""


def keychain_read(service: str) -> str:
    if shutil.which("security") is None:
        raise SecretUnavailable(
            "the macOS Keychain ('security') isn't available on this OS. Model keys are read only from the "
            "macOS Keychain, on purpose; setup, tests and every replay work without one"
        )
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecretUnavailable(f"could not run the Keychain lookup for '{service}'") from None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        raise SecretUnavailable(f"Keychain item '{service}' not found or unreadable") from None
    return value


def resolve_secret(ref: str) -> str:
    scheme, _, key = ref.partition(":")
    if scheme == "sim":
        from cu_sim.app import CREDENTIALS

        user = next(iter(CREDENTIALS))
        table = {"teller_username": user, "teller_password": CREDENTIALS[user]}
        if key not in table:
            raise SecretUnavailable(f"no synthetic credential '{key}'")
        return table[key]
    if scheme == "env":
        value = os.environ.get(key)
        if not value:
            raise SecretUnavailable(f"environment variable '{key}' not set")
        return value
    if scheme == "keychain":
        return keychain_read(key)
    raise SecretUnavailable(f"secret store '{scheme}' is a documented seam, not implemented here")
