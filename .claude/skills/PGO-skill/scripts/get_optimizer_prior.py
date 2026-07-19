#!/usr/bin/env python3
"""Filtered cross-benchmark prior for a benchmark's current top candidate findings.

Stage D of the PGO-skill memory pipeline: rather than handing the agent the
full global-optimizer-index.json (every optimizer, every benchmark), this joins
it against just the optimizers named by THIS benchmark's current top findings
(profile-summary.json) and returns one small annotation per candidate -- "has
this optimizer worked on any OTHER benchmark?" This benchmark's own history is
deliberately excluded here; that's what pgo-ledger-index.json / SKILL.md step 3
already covers, so nothing is shown twice.

Depends on profile-summary.json (run get_profile_summary.py first) and
state/global-optimizer-index.json (run build_global_index.py first) already
being present -- this script only reads and filters, it does not regenerate
either upstream stage.

Usage: get_optimizer_prior.py <benchmark>
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GPA_ROOT = SKILL_DIR.parents[2]
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"
GLOBAL_INDEX_PATH = SKILL_DIR / "state" / "global-optimizer-index.json"


def load_benchmark_dir(name: str) -> Path:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    path = GPA_ROOT / config[name]["dir"]
    if not path.is_dir():
        sys.exit(f"Benchmark dir not found: {path}")
    return path


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: get_optimizer_prior.py <benchmark>")
    name = sys.argv[1]
    bench_dir = load_benchmark_dir(name)

    summary_path = bench_dir / "gpa-database" / "profile-summary.json"
    if not summary_path.exists():
        sys.exit(f"No {summary_path} -- run get_profile_summary.py first")
    summary = json.loads(summary_path.read_text())

    if not GLOBAL_INDEX_PATH.exists():
        sys.exit(f"No {GLOBAL_INDEX_PATH} -- run build_global_index.py first")
    global_index = json.loads(GLOBAL_INDEX_PATH.read_text())["index"]

    candidates = []
    for finding in summary["findings"]:
        optimizer = finding["optimizer"]
        bucket = global_index.get(optimizer, {"benchmarks": {}})
        elsewhere = {b: v for b, v in bucket["benchmarks"].items() if b != name}
        kept_elsewhere = sum(
            1 for kernels in elsewhere.values() for v in kernels.values() if v["decision"] == "KEPT"
        )
        reverted_elsewhere = sum(
            1 for kernels in elsewhere.values() for v in kernels.values() if v["decision"] == "REVERTED"
        )
        candidates.append(
            {
                "kernel": finding["kernel"],
                "optimizer": optimizer,
                "ratio": finding["ratio"],
                "impactScore": finding.get("impactScore"),
                "priorElsewhere": {
                    "keptCount": kept_elsewhere,
                    "revertedCount": reverted_elsewhere,
                    "benchmarks": elsewhere,
                },
            }
        )

    print(json.dumps({"benchmark": name, "candidates": candidates}, indent=2))


if __name__ == "__main__":
    main()
