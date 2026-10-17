"""Fetch OpenRouter's own record of each discovery turn (upstream provider, served model, cost).

usage: uv run python scripts/fetch_generations.py evidence/discovery/<run-id>

Reads the generation ids from the run's compiled artifact provenance and writes generations.json
next to it. Uses the OpenRouter provider's key handling (Keychain only, never printed). Lookups only:
no model calls.
"""

import datetime as dt
import json
import sys
from pathlib import Path

from agent_hands.llm.openrouter_provider import OpenRouterProvider

run = Path(sys.argv[1])
draft = json.loads((run / "compiled_draft.json").read_text())
ids = draft["provenance"]["generation_ids"]
stats = OpenRouterProvider().generation_stats(ids)
out = {
    "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    "source": "GET https://openrouter.ai/api/v1/generation?id=<id> for each id in compiled_draft.json provenance",
    "why_this_file": "meta.json and run.jsonl were written while the account-number redaction pattern "
                     "matched the unix-time digits inside generation ids (gen-[ACCT …nnnn]-...). Fixed in "
                     "e837dfd; this run's files are left as written, and the intact ids come from the "
                     "artifact provenance, which is written verbatim after a lint.",
    "turns": stats,
    "total_cost_usd": round(sum(s.get("total_cost") or 0 for s in stats), 6),
    "upstream_providers": sorted({s.get("provider_name") for s in stats if s.get("provider_name")}),
    "models": sorted({s.get("model") for s in stats if s.get("model")}),
}
(run / "generations.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps({k: out[k] for k in ("total_cost_usd", "upstream_providers", "models")}))
for i, s in enumerate(stats, 1):
    print(f"turn {i}: {s.get('id')}  {s.get('provider_name')}  {s.get('model')}  ${s.get('total_cost')}")
