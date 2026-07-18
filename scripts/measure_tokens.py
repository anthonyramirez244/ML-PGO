#!/usr/bin/env python3
"""Snapshot Claude Code token usage via `npx ccusage session --json`.

ccusage has no per-project filter -- it reports every Claude Code session on
this machine, keyed by an opaque session id ("period") and a lastActivity
timestamp, not by working directory. So this script does NOT attempt to
isolate "this project's usage" automatically. Instead it dumps the full
session list with an explicit snapshot timestamp and the exact invocation
used, so a human (or a later diff between two snapshots) can identify which
session id(s) correspond to the work being measured.

Usage:
  measure_tokens.py <label>
    Writes reports/token-usage-<label>.json with the full ccusage session
    snapshot, the invocation used, and a snapshot timestamp.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GPA_ROOT = SCRIPT_DIR.parent
REPORTS_DIR = GPA_ROOT / "reports"

CCUSAGE_INVOCATION = ["npx", "ccusage", "session", "--json"]


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: measure_tokens.py <label>")
    label = sys.argv[1]

    result = subprocess.run(CCUSAGE_INVOCATION, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        sys.exit(f"ccusage exited {result.returncode}:\n{result.stderr}")

    sessions = json.loads(result.stdout)["session"]
    total_tokens_all_sessions = sum(s["totalTokens"] for s in sessions)

    snapshot = {
        "label": label,
        "snapshotTime": datetime.now(timezone.utc).isoformat(),
        "invocation": " ".join(CCUSAGE_INVOCATION),
        "caveat": (
            "ccusage has no per-project filter; totalTokensAllSessions sums every "
            "Claude Code session on this machine, not just PGO-skill work. Use "
            "sessions[].period + metadata.lastActivity to identify specific "
            "sessions relevant to a measurement window."
        ),
        "totalTokensAllSessions": total_tokens_all_sessions,
        "sessionCount": len(sessions),
        "sessions": sessions,
    }

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"token-usage-{label}.json"
    out_path.write_text(json.dumps(snapshot, indent=2))
    print(f"Wrote {out_path} ({total_tokens_all_sessions} tokens across {len(sessions)} sessions)", file=sys.stderr)


if __name__ == "__main__":
    main()
