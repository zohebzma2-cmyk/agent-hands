"""Audit an evidence directory for anything that must never be there. Prints counts, never values.

usage: uv run python scripts/audit_evidence.py DIR [DIR ...]
"""

import re
import sys
from pathlib import Path

from agent_hands.secrets import SecretUnavailable, keychain_read

secrets = {}
for service in ("openrouter-api-key", "anthropic-api-key"):
    try:
        secrets[f"keychain:{service}"] = keychain_read(service).encode()
    except SecretUnavailable:
        pass

LITERALS = {  # synthetic, but exactly what redaction must keep out
    "teller user id": b"teller01", "teller password": b"synthetic-teller-pass", "supervisor PIN": b"2468",
    "member name ADA SAMPLE": b"ADA SAMPLE", "member name SAMPLE, ADA": b"SAMPLE, ADA",
    "other members": b"PLACEHOLDER", "member CAROL EXAMPLE": b"CAROL", "member DAN SAMPLE": b"DAN SAMPLE",
    "balance 5,310.77": b"5,310.77", "balance 64.19": b"64.19", "address": b"Example Way", "DOB": b"04/12/1961",
    "phone": b"555-01", "balance 812.40": b"812.40", "balance 1,204.11": b"1,204.11",
}
PATTERNS = {
    "sk-or- key shape": rb"sk-or-[A-Za-z0-9_\-]{8,}", "sk-ant- key shape": rb"sk-ant-[A-Za-z0-9_\-]{8,}",
    # bare JSON numbers (": 900580,") are durations/counters; member numbers are always strings/text
    "home path /Users/": rb"/Users/", "member number 9xxxxx": rb'(?<![\w-])(?<!": )9\d{5}(?!\d)',
    "local temp path": rb"/var/folders/|pytest-of-",
    "account number (9-17 digits, not in a gen- id)": rb"(?<!gen-)(?<![\w-])\d{9,17}(?!\d)",
}

bad = 0
files = [p for d in sys.argv[1:] for p in sorted(Path(d).rglob("*")) if p.is_file()]
for p in files:
    data = p.read_bytes()
    if p.suffix == ".json":  # contract docs: params[].example is a synthetic sample chosen to differ
        data = re.sub(rb'"example": "[^"]*"', b'"example": ""', data)
    hits = [k for k, v in secrets.items() if v in data]
    hits += [k for k, rx in PATTERNS.items() if "key shape" in k and re.search(rx, data)]
    if p.suffix != ".md":  # README.md indexes are prose that names the patterns below; keys still checked
        hits += [k for k, v in LITERALS.items() if v in data and not (k == "supervisor PIN" and p.suffix == ".png")]
        hits += [k for k, rx in PATTERNS.items() if "key shape" not in k and re.search(rx, data)]
    if hits:
        bad += 1
        print(f"HIT {p}: {sorted(set(hits))}")
print(f"scanned {len(files)} files ({sum(p.suffix == '.png' for p in files)} screenshots), "
      f"{len(secrets)} keychain secret(s) checked: {'CLEAN' if not bad else f'{bad} file(s) with hits'}")
