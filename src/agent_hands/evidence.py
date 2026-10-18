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
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from agent_hands.redact import Redactor

# The page only ever reports cell texts back; matching against sensitive values happens here, in
# Python. Values (and never secrets or the API key) are not passed into page JavaScript.
_CELLS_JS = r"""
() => Array.from(document.querySelectorAll('td, th')).map((c, i) => {
  const norm = s => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  if (c.querySelector('td, th')) return null;  // only leaf cells
  return [i, norm(c.innerText), c.previousElementSibling ? norm(c.previousElementSibling.innerText) : ''];
}).filter(Boolean)
"""
_VALUEISH = re.compile(r"[\d$@,]")


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
        """``backend`` is who made the decisions: a provider name ("anthropic", "openrouter"), "fake"
        (tests only; refused for evidence/), or "replay" (no model)."""
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

    def write_raw(self, name: str, text: str) -> Path | None:
        """Verbatim write, for content that has already passed a redaction lint (artifacts)."""
        if self.dir is None:
            return None
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
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


def mask_locators(page: Page, values: list[str], *, structural: bool = True) -> list:
    """Locators for what a screenshot must hide.

    Inputs always. Cells showing any of ``values``. With ``structural``, also every cell that looks
    like data (digits, money, emails, "LAST, FIRST") or follows a "Label:" cell: deny by default for
    values, while labels, headings and buttons stay readable.
    """
    masks = []
    for f in page.frames:
        if f.is_detached():
            continue
        masks.append(f.locator("input:not([type=button]):not([type=submit]):not([type=hidden]), select"))
        try:
            cells = f.evaluate(_CELLS_JS)
        except Exception:
            continue
        loc = f.locator("td, th")
        for i, text, prev in cells:
            if not text:
                continue
            hit = any(v and v in text for v in values)
            if hit or (structural and (_VALUEISH.search(text) or (prev.endswith(":") and not text.endswith(":")))):
                masks.append(loc.nth(i))
    return masks
