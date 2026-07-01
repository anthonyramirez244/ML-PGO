#!/usr/bin/env python3
"""Parse a GPA gpa.advice file into structured, deduped, ranked JSON."""

import argparse
import json
import re
import sys

KERNEL_RE = re.compile(r"^GPU Kernel (.+): (\d+)\s*$")
SECTION_NAMES = {"Code Optimizers", "Parallel Optimizers", "Binary Optimizers"}
APPLY_RE = re.compile(
    r"^\s*Apply (\w+) optimization, ratio ([\d.]+)%,\s*estimate speedup (inf|[\d.]+)x\s*$"
)
FROM_TO_RE = re.compile(r"^\s*(?:From|To) .* at (.+):(\d+)\s*$")
ADDR_RE = re.compile(r"^\s*0x[0-9a-fA-F]+ at Line (\d+)(?: in Loop at Line (\d+))?\s*$")
DASHES_RE = re.compile(r"^-{5,}\s*$")
STARS_RE = re.compile(r"^\*{5,}\s*$")
NUMBERED_BLAME_RE = re.compile(r"^\s*\d+\.\s*Hot BLAME")
NESTED_BLAME_RE = re.compile(r"^\s*Hot BLAME")
BLAME_RATIO_RE = re.compile(r"Hot BLAME \S+ code, ratio ([\d.]+)%")
ADJUST_RE = re.compile(r"^\s*Adjust ")


def is_structural(line: str) -> bool:
    return bool(
        FROM_TO_RE.match(line)
        or ADDR_RE.match(line)
        or DASHES_RE.match(line)
        or STARS_RE.match(line)
        or APPLY_RE.match(line)
        or KERNEL_RE.match(line)
        or NUMBERED_BLAME_RE.match(line)
        or NESTED_BLAME_RE.match(line)
        or ADJUST_RE.match(line)
        or line.strip() in SECTION_NAMES
    )


def parse_kernel_blocks(text: str):
    """Return a list of kernel blocks: {signature, entries: [...]}."""
    lines = text.splitlines()
    blocks = []
    cur_block = None
    cur_section = None
    cur_entry = None
    pending_file = None
    cur_blame_ratio = None  # ratio of the innermost "Hot BLAME ... ratio X%" line
                              # currently in scope -- governs the location(s) that
                              # follow, until the next Hot BLAME line updates it

    def flush_entry():
        nonlocal cur_entry
        if cur_entry is not None and cur_block is not None:
            cur_entry["description"] = " ".join(cur_entry["description_lines"]).strip()
            del cur_entry["description_lines"]
            # highest-contributing location first -- the parser previously sorted
            # by (file, line), which does NOT correlate with which location is
            # actually responsible for the most stall cycles within this finding
            cur_entry["locations"] = sorted(
                cur_entry["locations"],
                key=lambda loc: (-(loc.get("ratio") or 0.0), loc["file"], loc["line"]),
            )
            cur_block["entries"].append(cur_entry)
        cur_entry = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        m = KERNEL_RE.match(line)
        if m:
            flush_entry()
            cur_section = None
            cur_block = {"signature": m.group(1), "entries": []}
            blocks.append(cur_block)
            continue

        if line.strip() in SECTION_NAMES:
            flush_entry()
            cur_section = line.strip()
            continue

        m = APPLY_RE.match(line)
        if m and cur_block is not None:
            flush_entry()
            speedup_raw = m.group(3)
            cur_entry = {
                "optimizer": m.group(1),
                "category": cur_section,
                "ratio": float(m.group(2)),
                "speedup": None if speedup_raw == "inf" else float(speedup_raw),
                "description_lines": [],
                "locations": [],
            }
            pending_file = None
            cur_blame_ratio = None
            continue

        if cur_entry is None:
            continue

        m = BLAME_RATIO_RE.search(line)
        if m:
            cur_blame_ratio = float(m.group(1))
            continue

        m = FROM_TO_RE.match(line)
        if m:
            pending_file = m.group(1)
            continue

        m = ADDR_RE.match(line)
        if m and pending_file is not None:
            loc = {"file": pending_file, "line": int(m.group(1))}
            if m.group(2):
                loc["loopLine"] = int(m.group(2))
            if cur_blame_ratio is not None:
                loc["ratio"] = cur_blame_ratio
            cur_entry["locations"].append(loc)
            continue

        if is_structural(line):
            continue

        stripped = line.strip()
        if stripped:
            cur_entry["description_lines"].append(stripped)

    flush_entry()
    return blocks


def dedupe_blocks(blocks):
    """Collapse kernel blocks that are exact repeats (same signature + same
    optimizer/ratio/speedup set) into one, tracking occurrence count."""
    deduped = []
    fingerprints = {}

    for block in blocks:
        fp_entries = tuple(
            sorted((e["optimizer"], e["ratio"], e["speedup"]) for e in block["entries"])
        )
        fp = (block["signature"], fp_entries)

        if fp in fingerprints:
            existing = fingerprints[fp]
            existing["occurrences"] += 1
            for new_e, existing_e in zip(block["entries"], existing["entries"]):
                seen = {(loc["file"], loc["line"]) for loc in existing_e["locations"]}
                for loc in new_e["locations"]:
                    if (loc["file"], loc["line"]) not in seen:
                        existing_e["locations"].append(loc)
            continue

        deduped_block = {
            "signature": block["signature"],
            "occurrences": 1,
            "entries": block["entries"],
        }
        fingerprints[fp] = deduped_block
        deduped.append(deduped_block)

    return deduped


def build_findings(deduped_blocks):
    findings = []
    informational = []

    for block in deduped_blocks:
        for entry in block["entries"]:
            if entry["ratio"] <= 0.0:
                continue

            record = {
                "kernel": block["signature"],
                "occurrences": block["occurrences"],
                "category": entry["category"],
                "optimizer": entry["optimizer"],
                "ratio": entry["ratio"],
                "speedup": entry["speedup"],
                "description": entry["description"],
                "locations": entry["locations"],
            }

            if entry["speedup"] is None:
                informational.append(record)
            else:
                record["impactScore"] = round(
                    (entry["ratio"] / 100.0) * max(entry["speedup"] - 1.0, 0.0), 6
                )
                findings.append(record)

    findings.sort(key=lambda r: r["impactScore"], reverse=True)
    informational.sort(key=lambda r: r["ratio"], reverse=True)
    return findings, informational


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("advice_path", help="Path to a gpa.advice file")
    parser.add_argument(
        "--top", type=int, default=None, help="Limit output to the top N findings by impact score"
    )
    args = parser.parse_args()

    with open(args.advice_path, "r") as f:
        text = f.read()

    blocks = parse_kernel_blocks(text)
    deduped_blocks = dedupe_blocks(blocks)
    findings, informational = build_findings(deduped_blocks)

    if args.top is not None:
        findings = findings[: args.top]

    result = {
        "sourceFile": args.advice_path,
        "kernelBlockCount": len(deduped_blocks),
        "findings": findings,
        "informational": informational,
    }

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
