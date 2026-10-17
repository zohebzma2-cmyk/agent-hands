"""Scan every blob in the git history for secrets and local paths. Prints counts and paths, never values.

usage: uv run python scripts/scan_history.py
"""

import re
import subprocess

from agent_hands.secrets import SecretUnavailable, keychain_read

secrets = {}
for service in ("openrouter-api-key", "anthropic-api-key"):
    try:
        secrets[service] = keychain_read(service).encode()
    except SecretUnavailable:
        pass
patterns = {
    "sk-or- key shape": re.compile(rb"sk-or-[A-Za-z0-9_\-]{8,}"),
    "sk-ant- key shape": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}"),
    "/Users/ path": re.compile(rb"/Users/"),
}

objects = subprocess.run(["git", "rev-list", "--all", "--objects"], capture_output=True, check=True).stdout
blobs: dict[str, str] = {}
for line in objects.decode().splitlines():
    sha, _, path = line.partition(" ")
    if path:
        blobs.setdefault(sha, path)
hits: dict[str, set[str]] = {}
for sha, path in blobs.items():
    kind = subprocess.run(["git", "cat-file", "-t", sha], capture_output=True).stdout.strip()
    if kind != b"blob":
        continue
    data = subprocess.run(["git", "cat-file", "-p", sha], capture_output=True).stdout
    for name, value in secrets.items():
        if value in data:
            hits.setdefault(f"keychain:{name}", set()).add(path)
    for name, rx in patterns.items():
        if rx.search(data):
            hits.setdefault(name, set()).add(path)
commits = subprocess.run(["git", "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip()
print(f"{commits} commits, {len(blobs)} objects, {len(secrets)} keychain secret(s) compared")
for name, paths in sorted(hits.items()):
    print(f"HIT {name}: {sorted(paths)}")
print("CLEAN" if not hits else f"{len(hits)} kind(s) of hits")
