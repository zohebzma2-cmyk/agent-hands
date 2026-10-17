# agent-hands

Take-home for interface.ai (Applied AI Engineer, Business Automation). Brief: `materials/brief.pdf`
(local only, gitignored, not published).

## Goal

A small but real computer-use automation layer for legacy bank back-office UIs that have no API:

1. An LLM discovers how to do a goal in a live UI (observe → decide → act).
2. The successful run is compiled into a typed, versioned **capability artifact**.
3. The artifact **replays deterministically** with no model in the loop. Replay returns a result
   that keeps business outcomes, recoverable conditions and hard failures apart.
4. When stuck, it **hands the same live browser session to a human**, records what they did, and resumes.
5. It stays inside an **allowlist** and never persists secrets or raw sensitive data.

The target is a local, deliberately hostile stand-in for a credit union back office (`target/`),
with switches that inject runtime faults.

Graded on judgment: artifact schema, deterministic replay plus error taxonomy, safety and escalation.
A thin but real version of every core requirement beats a polished subset. Every mock is deliberate
and documented. The discovery run in `evidence/` must be a real LLM run.

## Stack

Python 3.12, uv, Playwright (Chromium), pydantic v2, pytest. Keep dependencies small.
LLM: Claude Opus 5.5 behind a provider interface. Default: OpenRouter (`anthropic/claude-opus-5.5`,
routed to Anthropic only). Direct: Anthropic (`claude-opus-5-5`).
Irreversible steps are approved live by a human at the terminal (discovery) or by an explicit, run-bound caller
approval (replay); unattended replay stops at them.

## Commands

Keep this section in step with what actually exists.

```
uv sync                                   # install
uv run playwright install chromium        # browser
uv run pytest                             # tests (offline, fake LLM backend)
uv run hands target                       # start the hostile target app
uv run hands discover --spec specs/open_sub_account.json -p k=v ... --headed [--provider openrouter|anthropic]
uv run hands validate <draft> -p k=v ...  # re-run a draft's validation (human approval), promote it
uv run hands replay <artifact> -p k=v ... [--approve s13] [--fault name[:mode][:ms][@route]]
                    [--tenant lakeside] [--no-override] [--operator terminal --headed] [--allow-draft]
uv run hands stability <artifact> -p k=v ... --approve s13 --runs 20
uv run hands show <artifact>
uv run python scripts/audit_evidence.py evidence   # secrets/PII audit of evidence
uv run python scripts/scan_history.py              # secrets/paths across git history
```

## Hard rules

- **Synthetic data only.** Fake members, fake account numbers, fake credentials. No real PII ever.
- **No secrets in the repo.** Model keys come only from the macOS Keychain (`openrouter-api-key`,
  the default provider, or `anthropic-api-key`), read inside the provider when a live run starts.
  The audit scripts also read them, only to compare against files. If a read fails, stop and tell
  me; never look for a key anywhere else (no env vars, no files, no ~/.zshenv). Never print,
  log or write a key, including evidence, traces, fixtures, exception messages and test output.
- **Never push without asking me first.** Commit locally after every step that works.
  Don't email anyone.
- **Nothing outside the allowlist.** The agent and replay engine act only on allowlisted
  origins, routes and action types. The policy layer enforces this in code, not in the prompt.
- **The fake LLM backend is for tests only.** Nothing it produces may go in `evidence/`.
- **Replay is deterministic.** No model calls, no randomness, no wall-clock-dependent branching
  in the replay decision path. Timeouts are bounded and come from config.
- Redact before persisting: every log, trace, screenshot caption and artifact goes through the
  redactor.

## Workflow

- Run the `reviewer` subagent on meaningful diffs before committing milestones.
