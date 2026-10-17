"""The single way anything about a run reaches disk.

Every event, JSON document and screenshot goes through a ``RunLog``, which:

- scrubs every string through the run's redactor (contract-driven, then patterns),
- masks sensitive regions and all form inputs in screenshots,
- refuses to write into ``evidence/`` for runs driven by the fake LLM backend (tests only), or for
  replays of artifacts that a fake backend produced.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from agent_hands.redact import Redactor

# Cells to black out in evidence screenshots: anything that looks like data (digits, money, emails,
# "LAST, FIRST"), any cell that follows a "Label:" cell, and any element showing a contract value.
# Deny-by-default for values; labels, headings and buttons stay readable.
_MASK_JS = r"""
(values) => {
  const norm = s => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const valueish = /[\d$@,]/;
  const cells = Array.from(document.querySelectorAll('td, th'));
  const out = [];
  cells.forEach((c, i) => {
    if (c.querySelector('td, th')) return;  // only leaf cells
    const t = norm(c.innerText);
    if (!t) return;
    const prev = c.previousElementSibling ? norm(c.previousElementSibling.innerText) : '';
    if (valueish.test(t) || prev.endsWith(':') || values.some(v => v && t.includes(v))) out.push(i);
  });
  return out;
}
"""

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPO_ROOT / "evidence"


class EvidenceGuardError(RuntimeError):
    pass


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class RunLog:
    def __init__(self, run_dir: Path | None, redactor: Redactor, *, backend: str) -> None:
        """``backend`` is who made the decisions: "anthropic", "fake", or "replay" (no model)."""
        if run_dir is not None and backend == "fake" and _is_under(run_dir, EVIDENCE_ROOT):
            raise EvidenceGuardError("runs driven by the fake LLM backend may not write to evidence/")
        self.dir = run_dir
        self.redactor = redactor
        self.backend = backend
        self.seq = 0
        self.events: list[dict[str, Any]] = []
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        self.seq += 1
        rec = {"seq": self.seq, "t": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"), "kind": kind}
        rec.update(self.redactor.scrub_obj(data))
        self.events.append(rec)
        if self.dir is not None:
            with (self.dir / "run.jsonl").open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=False) + "\n")
        return rec

    def write_json(self, name: str, obj: Any, *, already_redacted: bool = False) -> Path | None:
        if self.dir is None:
            return None
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = obj if already_redacted else self.redactor.scrub_obj(obj)
        path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        return path

    def write_text(self, name: str, text: str) -> Path | None:
        if self.dir is None:
            return None
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.scrub(text))
        return path

    def screenshot(self, page: Page, name: str, mask_css: list[tuple[list[str], str]] | None = None) -> str | None:
        """Masked screenshot: inputs, data-looking cells, contract values and ``mask_css`` regions."""
        if self.dir is None:
            return None
        masks = mask_locators(page, self.redactor.sensitive_values())
        for frame_names, css in mask_css or []:
            for f in page.frames:
                if not frame_names or f.name in frame_names:
                    masks.append(f.locator(css))
        path = self.dir / "screens" / f"{self.seq:03d}-{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(path), mask=masks, mask_color="#222222", timeout=5000)
        except Exception as e:  # a failing screenshot must never fail the run
            self.event("screenshot_failed", name=name, error=self.redactor.scrub_exception(e))
            return None
        return str(path.relative_to(self.dir))


def mask_locators(page: Page, values: list[str]) -> list:
    """Locators for everything an evidence screenshot must hide (see ``_MASK_JS``)."""
    masks = []
    for f in page.frames:
        if f.is_detached():
            continue
        masks.append(f.locator("input:not([type=button]):not([type=submit]):not([type=hidden]), select"))
        try:
            idx = f.evaluate(_MASK_JS, values)
        except Exception:
            continue
        cells = f.locator("td, th")
        masks.extend(cells.nth(i) for i in idx)
    return masks
