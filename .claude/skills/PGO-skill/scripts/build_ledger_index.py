#!/usr/bin/env python3
"""Derive <benchmark-dir>/gpa-database/pgo-ledger-index.json from
pgo-ledger.md, for an O(1) "has kernel+optimizer already been tried?" lookup.

pgo-ledger.md remains the single source of truth -- this index is always
fully rebuilt from it (never hand-edited), and is skipped/regenerated
automatically based on the ledger's content hash. Attribution of a ledger
entry to a specific kernel is done by checking benchmarks.json's known
"kernels" vocabulary for that benchmark against the entry's text, rather than
guessing kernel names via open-ended regex -- an entry whose kernel or
optimizer can't be attributed this way is skipped rather than indexed
incorrectly.

Usage: build_ledger_index.py <benchmark>
"""

import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GPA_ROOT = SKILL_DIR.parents[2]
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"

ENTRY_HEADER_RE = re.compile(r"^### (.+?) — .+$", re.MULTILINE)
DECISION_RE = re.compile(r"^- Decision:\s*(KEPT|REVERTED)", re.MULTILINE)
OPTIMIZER_RE = re.compile(r"\bGPU\w+Optimizer\b")


def load_benchmark(name: str) -> tuple[Path, list[str]]:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    cfg = config[name]
    bench_dir = GPA_ROOT / cfg["dir"]
    if not bench_dir.is_dir():
        sys.exit(f"Benchmark dir not found: {bench_dir}")
    # benchmarks.json's "kernels" entries look like "Kernel(" -- strip the
    # trailing "(" so they match plain-text mentions in the ledger's prose.
    kernel_vocab = [k.rstrip("(") for k in cfg.get("kernels", [])]
    return bench_dir, kernel_vocab


def parse_entries(ledger_text: str) -> list[dict]:
    headers = list(ENTRY_HEADER_RE.finditer(ledger_text))
    entries = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(ledger_text)
        entries.append({"timestamp": m.group(1), "text": ledger_text[start:end]})
    return entries


def build_index(ledger_text: str, kernel_vocab: list[str]) -> dict:
    index: dict[str, dict[str, dict]] = {}
    skipped_unattributed = 0
    for entry in parse_entries(ledger_text):
        text = entry["text"]
        decision_m = DECISION_RE.search(text)
        if not decision_m:
            continue  # facts-only entry -- agent hasn't logged a KEPT/REVERTED decision yet
        decision = decision_m.group(1)
        optimizers = sorted(set(OPTIMIZER_RE.findall(text)))
        kernels_mentioned = [k for k in kernel_vocab if k in text]
        if not optimizers or not kernels_mentioned:
            skipped_unattributed += 1
            continue
        for kernel in kernels_mentioned:
            for optimizer in optimizers:
                index.setdefault(kernel, {})[optimizer] = {
                    "decision": decision,
                    "lastUpdated": entry["timestamp"],
                }
    if skipped_unattributed:
        print(
            f"Note: {skipped_unattributed} ledger entr{'y' if skipped_unattributed == 1 else 'ies'} "
            f"could not be attributed to a known kernel+optimizer and were skipped (not indexed, "
            f"not lost -- still readable in pgo-ledger.md directly).",
            file=sys.stderr,
        )
    return index


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: build_ledger_index.py <benchmark>")
    name = sys.argv[1]
    bench_dir, kernel_vocab = load_benchmark(name)

    ledger_path = bench_dir / "gpa-database" / "pgo-ledger.md"
    index_path = bench_dir / "gpa-database" / "pgo-ledger-index.json"

    if not ledger_path.exists():
        index_path.parent.mkdir(exist_ok=True)
        index_path.write_text(json.dumps({"sourceFile": str(ledger_path), "sourceHash": None, "index": {}}, indent=2))
        print(f"No ledger yet at {ledger_path}; wrote empty index at {index_path}", file=sys.stderr)
        return

    ledger_text = ledger_path.read_text()
    source_hash = hashlib.sha256(ledger_text.encode()).hexdigest()

    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text())
        except json.JSONDecodeError:
            existing = None
        if existing and existing.get("sourceHash") == source_hash:
            print(f"(up to date -- {index_path} already reflects current pgo-ledger.md)", file=sys.stderr)
            return

    index = build_index(ledger_text, kernel_vocab)
    index_path.write_text(json.dumps({"sourceFile": str(ledger_path), "sourceHash": source_hash, "index": index}, indent=2))
    pair_count = sum(len(v) for v in index.values())
    print(f"Wrote {index_path} ({pair_count} kernel+optimizer pairs)", file=sys.stderr)


if __name__ == "__main__":
    main()
