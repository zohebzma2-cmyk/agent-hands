#!/usr/bin/env bash
# Records the human-in-the-loop evidence runs back to back. Run from the repo root in an interactive terminal:
#
#   bash scripts/record_evidence.sh
#
# 1. validate + promote the discovered draft     (you type: approve)          headless, ~20 s
# 2. human handoff on the live session            (you type: take ... resume)  headed,  ~1-2 min
# 3. approved replays + 20x stability, unattended (nothing to type)            headless, ~3 min
#
# Steps 2 and 3 pass --approve s13: running this script is your explicit approval of the commit in
# each of those runs (recorded as your user, channel caller_token, bound to that run's own id).
set -euo pipefail

DRAFT=evidence/discovery/disc-20261017T200007/compiled_draft.json
ART=artifacts/cu_core.open_sub_account@1.0.0.json
OUT_REPLAY=${OUT_REPLAY:-evidence/replay}
OUT_HANDOFF=${OUT_HANDOFF:-evidence/handoff}
OUT_STAB=${OUT_STAB:-evidence/stability}
RUNS=${RUNS:-20}
# params for member $1 into the array P (an array, so "Rainy day" stays one argument)
params() { P=(-p "member_id=$1" -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00); }
banner() { printf '\n\033[1m==== %s\033[0m\n' "$*"; }

if [[ "${SKIP_VALIDATE:-}" != 1 ]]; then
  banner "1/3  Validate the discovered draft (no model). Type: approve"
  params 900001; uv run hands validate "$DRAFT" "${P[@]}"
fi

if [[ "${SKIP_HANDOFF:-}" != 1 ]]; then
  banner "2/3  Handoff. When the INTERVENTION box appears:"
  cat <<'EOT'
       a) type: take
       b) in the browser window, Supervisor PIN: 2468, click "Approve Override"
       c) back here, type: resume
EOT
  params 900003
  uv run hands replay "$ART" "${P[@]}" --approve s13 --fault unknown_dialog --operator terminal --headed \
      --out "$OUT_HANDOFF" --run-id handoff-supervisor-override || echo "(handoff run exited $?)"
fi

banner "3/3  Approved replays and stability (unattended; nothing to type)"
replay() { local id=$1 mid=$2; shift 2; params "$mid"
  uv run hands replay "$ART" "${P[@]}" --approve s13 --out "$OUT_REPLAY" --run-id "$id" "$@" >/dev/null \
    && echo "  $id: succeeded" || echo "  $id: exit $? (see $OUT_REPLAY/$id/result.json)"; }
replay approved-happy-path-900003 900003
replay approved-announcement-dialog 900004 --fault announcement
replay approved-session-expired 900002 --fault session_expired
replay approved-http500-member-detail 900001 --fault http500
params 900001
uv run hands stability "$ART" "${P[@]}" --approve s13 --runs "$RUNS" --out "$OUT_STAB" | tail -n 25 || true

banner "Done. Run scripts/audit_evidence.py evidence."
