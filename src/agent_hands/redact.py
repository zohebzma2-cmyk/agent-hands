"""Redaction for everything that leaves memory: logs, results, evidence, snapshots.

Two layers, in order:

1. **Contract-driven.** The capability declares the sensitivity of every param, secret and output.
   Exact values marked ``pii`` or ``secret`` are replaced with a named token (``[PII:member_id]``),
   so logs stay debuggable without the value.
2. **Pattern backstop.** Regexes for things nobody declared: SSNs, long account numbers, emails,
   phone numbers, dates of birth, money amounts and API keys.

Process-wide secrets (the model API key) are registered once and scrubbed by every redactor and by
the logging filter, wherever they turn up.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from agent_hands.artifact.schema import Sensitivity

if TYPE_CHECKING:
    from agent_hands.artifact.schema import CapabilityArtifact

MIN_VALUE_LEN = 3  # shorter values would shred unrelated text ("step 1 of 10")
_PROCESS_SECRETS: set[str] = set()

Replacement = str | Callable[[re.Match[str]], str]
Pattern = tuple[str, re.Pattern[str], Replacement]

DEFAULT_PATTERNS: tuple[Pattern, ...] = (
    ("api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[SECRET]"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    ("phone", re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"), "[PHONE]"),
    (
        "dob",
        re.compile(r"(?i)\b(dob|date of birth|birth\s?date)(\W{0,3})\d{1,2}/\d{1,2}/\d{2,4}"),
        r"\1\2[DOB]",
    ),
    ("account", re.compile(r"\b\d{9,17}\b"), lambda m: f"[ACCT …{m.group()[-4:]}]"),
    ("money", re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?"), "[AMOUNT]"),
)

_TOKEN = {Sensitivity.PII: "PII", Sensitivity.SECRET: "SECRET"}


def register_process_secret(value: str) -> None:
    """Scrub ``value`` everywhere, for the life of the process. Used for the model API key."""
    if value and len(value) >= 8:
        _PROCESS_SECRETS.add(value)


def clear_process_secrets() -> None:
    _PROCESS_SECRETS.clear()


def _renderings(value: Any) -> set[str]:
    """The ways a value is likely to appear on a page or in a log."""
    out = {str(value)}
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        out.add(f"{value:,}")
        if isinstance(value, Decimal):
            out.add(f"{value:.2f}")
            out.add(f"{value:,.2f}")
    return {r for r in out if len(r) >= MIN_VALUE_LEN}


def compile_patterns(specs: Sequence[tuple[str, str]]) -> tuple[Pattern, ...]:
    """Profile-declared identifier formats, e.g. ("member_number", r"\\b9\\d{5}\\b")."""
    return tuple((name, re.compile(rx), f"[{name.upper()}]") for name, rx in specs)


class Redactor:
    def __init__(self, patterns: Sequence[Pattern] = DEFAULT_PATTERNS) -> None:
        self._patterns = tuple(patterns)
        self._values: dict[str, str] = {}
        self._lower: dict[str, str] = {}
        self._compiled: re.Pattern[str] | None = None
        self._compiled_for: frozenset[str] = frozenset()

    @classmethod
    def from_contract(
        cls,
        artifact: CapabilityArtifact,
        *,
        params: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
        outputs: Mapping[str, Any] | None = None,
        patterns: Sequence[Pattern] = DEFAULT_PATTERNS,
    ) -> Redactor:
        r = cls(tuple(patterns))
        for spec in artifact.params:
            if params and spec.name in params:
                r.add(spec.name, params[spec.name], spec.sensitivity)
        for name, value in (secrets or {}).items():
            r.add(name, value, Sensitivity.SECRET)
        for out in artifact.outputs:
            if outputs and out.name in outputs:
                r.add(out.name, outputs[out.name], out.sensitivity)
        return r

    def add(self, name: str, value: Any, sensitivity: Sensitivity) -> None:
        if sensitivity not in _TOKEN or value is None:
            return
        token = f"[{_TOKEN[sensitivity]}:{name}]"
        for rendering in _renderings(value):
            self._values[rendering] = token
            self._lower.setdefault(rendering.lower(), token)
        self._compiled = None

    def _matcher(self) -> re.Pattern[str] | None:
        keys = frozenset(self._values) | frozenset(_PROCESS_SECRETS)
        if self._compiled is None or keys != self._compiled_for:
            if not keys:
                self._compiled = None
            else:
                ordered = sorted(keys, key=len, reverse=True)  # longest first wins overlaps
                # whole tokens only (no eating the middle of a word), any case ("Ada Sample")
                alts = "|".join(re.escape(k) for k in ordered)
                self._compiled = re.compile(f"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)
            self._compiled_for = keys
        return self._compiled

    def sensitive_values(self) -> list[str]:
        """Every declared-sensitive rendering this redactor knows, plus process secrets."""
        return sorted(set(self._values) | set(_PROCESS_SECRETS), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        matcher = self._matcher()
        if matcher is not None:
            text = matcher.sub(lambda m: self._lower.get(m.group().lower(), "[SECRET]"), text)
        for _, rx, repl in self._patterns:
            text = rx.sub(repl, text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        """A redacted, JSON-friendly copy. The input is never modified."""
        if isinstance(obj, BaseModel):
            return self.scrub_obj(obj.model_dump(mode="json"))
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, Mapping):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        if isinstance(obj, Decimal):
            return self.scrub(str(obj))
        if isinstance(obj, int) and not isinstance(obj, bool) and str(obj) in self._values:
            return self._values[str(obj)]
        return obj

    def scrub_exception(self, exc: BaseException) -> str:
        return self.scrub(f"{type(exc).__name__}: {exc}")


_VALUEISH = re.compile(r"[\d$@,]")


def mask_cells(text: str) -> str:
    """Structural masking for free text read off a table-layout page (innerText).

    innerText renders table cells tab-separated. A cell is masked when it looks like data (digits,
    money, emails, "LAST, FIRST") or when it follows a label cell ending in ':'. Labels, headings and
    button text survive, so a failure summary still says where the page was.
    """
    out_lines = []
    for line in text.splitlines():
        cells = line.split("\t")
        masked, prev = [], ""
        for cell in cells:
            c = cell.strip()
            label = c.endswith(":")
            if c and (_VALUEISH.search(c) or (prev.endswith(":") and not label)):
                masked.append("▒")
            else:
                masked.append(cell)
            prev = c  # the immediately preceding cell, even if empty (an input sits there)
        out_lines.append("\t".join(masked))
    return "\n".join(out_lines)


def mask_value(value: Any, mode: str) -> str | None:
    """Apply an output's declared redaction mode for persistence."""
    if mode == "none":
        return str(value)
    if mode == "last4":
        return "••••" + str(value)[-4:]
    if mode == "drop":
        return None
    return "[REDACTED]"  # "mask" and anything unknown: fail closed


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        r = Redactor()
        record.msg = r.scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = r.scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_filter(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger("agent_hands")
    if not any(isinstance(f, _ScrubFilter) for f in target.filters):
        target.addFilter(_ScrubFilter())
