#!/usr/bin/env python3
"""Build/run/time/correctness-check a GPA-Benchmark case, before and after an
optimization. Two subcommands:

  pgo_bench.py baseline <benchmark>   capture pre-edit reference state
  pgo_bench.py compare  <benchmark>   re-measure after an edit, diff vs baseline,
                                      append a facts-only entry to the ledger
"""

import argparse
import glob
import json
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GPA_ROOT = SKILL_DIR.parents[2]  # .claude/skills/PGO-skill -> .claude/skills -> .claude -> GPA
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"
LEDGER_TEMPLATE = SKILL_DIR / "assets" / "ledger-template.md"

QDSTRM_IMPORTER_CANDIDATES = [
    "/usr/lib/nsight-systems/host-linux-x64/QdstrmImporter",
    "/opt/nvidia/nsight-systems/*/host-linux-x64/QdstrmImporter",
]

DEFAULT_RUN_TIMEOUT_SECONDS = 60  # generous vs. most benchmarks' ~1-5s runtime; a bad edit
                                  # (e.g. a broken loop-termination condition) can hang the
                                  # binary forever, so every run must be bounded. Slower
                                  # benchmarks (e.g. cfd, ~35s/run) should set their own
                                  # "runTimeoutSeconds" in benchmarks.json -- otherwise a
                                  # legitimately-slower-but-finite run after an edit could
                                  # get misclassified as a hang.


class BenchmarkTimeout(Exception):
    pass


def load_benchmark(name: str) -> dict:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    cfg = dict(config[name])
    cfg["path"] = GPA_ROOT / cfg["dir"]
    if not cfg["path"].is_dir():
        sys.exit(f"Benchmark dir not found: {cfg['path']}")
    return cfg


def build(cfg: dict) -> None:
    result = subprocess.run(cfg["buildCmd"], cwd=cfg["path"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Build failed for {cfg['dir']}:\n{result.stdout}\n{result.stderr}")


def timed_runs(cfg: dict) -> tuple[float, str]:
    """Run the benchmark N times, return (median wall-clock seconds, last run's stdout)."""
    timeout = cfg.get("runTimeoutSeconds", DEFAULT_RUN_TIMEOUT_SECONDS)
    times = []
    last_stdout = ""
    for _ in range(cfg.get("runs", 3)):
        start = time.perf_counter()
        try:
            result = subprocess.run(
                cfg["runCmd"], cwd=cfg["path"], capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise BenchmarkTimeout(
                f"run did not finish within {timeout}s "
                f"(likely an infinite loop or broken termination condition introduced by the edit)"
            )
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        last_stdout = result.stdout + result.stderr
    return statistics.median(times), last_stdout


def find_qdstrm_importer() -> str | None:
    path = shutil.which("QdstrmImporter")
    if path:
        return path
    for candidate in QDSTRM_IMPORTER_CANDIDATES:
        matches = glob.glob(candidate)
        if matches:
            return matches[0]
    return None


def profile_kernels(cfg: dict) -> tuple[dict, str]:
    """Best-effort per-kernel GPU time via nsys -> sqlite export -> direct SQL query.

    Returns (kernel_times_ns, note). kernel_times_ns is {} when kernel-level
    activity genuinely isn't available on this platform (a known limitation on
    some WSL2/driver combinations) -- callers must not treat that as an error,
    just report end-to-end timing instead and surface `note` to the user.
    """
    report_base = cfg["path"] / "pgo-bench-report"
    qdstrm_path = report_base.with_suffix(".qdstrm")
    nsysrep_path = report_base.with_suffix(".nsys-rep")
    sqlite_path = report_base.with_suffix(".sqlite")
    for p in (qdstrm_path, nsysrep_path, sqlite_path):
        p.unlink(missing_ok=True)

    profile_cmd = ["nsys", "profile", "--trace=cuda", "-f", "true", "-o", str(report_base)] + cfg["runCmd"]
    try:
        subprocess.run(profile_cmd, cwd=cfg["path"], capture_output=True, text=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {}, f"nsys profile failed to run: {e}"

    if not nsysrep_path.exists() and qdstrm_path.exists():
        importer = find_qdstrm_importer()
        if not importer:
            return {}, "nsys produced a .qdstrm but no QdstrmImporter binary was found to convert it"
        subprocess.run([importer, f"--input-file={qdstrm_path}"], capture_output=True, text=True, timeout=180)

    if not nsysrep_path.exists():
        return {}, "nsys produced no usable report file"

    subprocess.run(
        ["nsys", "export", "--type", "sqlite", "--force-overwrite=true",
         "--output", str(sqlite_path), str(nsysrep_path)],
        capture_output=True, text=True, timeout=180,
    )
    if not sqlite_path.exists():
        return {}, "nsys export did not produce a sqlite file"

    kernel_times: dict = {}
    note = "ok"
    try:
        con = sqlite3.connect(sqlite_path)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='CUPTI_ACTIVITY_KIND_KERNEL'")
        if not cur.fetchone():
            note = (
                "nsys captured no GPU kernel activity records on this platform "
                "(known limitation on some WSL2/driver combinations) -- "
                "end-to-end timing is still valid, local kernel timing is not"
            )
        else:
            cur.execute(
                """
                SELECT s.value, SUM(k."end" - k.start)
                FROM CUPTI_ACTIVITY_KIND_KERNEL k
                JOIN StringIds s ON k.shortName = s.id
                GROUP BY s.value
                """
            )
            for name, total_ns in cur.fetchall():
                for target in cfg["kernels"]:
                    if target in name:
                        kernel_times[target] = kernel_times.get(target, 0.0) + total_ns
            if not kernel_times:
                note = "nsys captured kernel activity but none matched the configured kernel name(s)"
        con.close()
    finally:
        for p in (qdstrm_path, nsysrep_path, sqlite_path):
            p.unlink(missing_ok=True)

    return kernel_times, note


def check_correctness(cfg: dict, gpa_db_dir: Path, is_baseline: bool, run_stdout: str) -> tuple[str, str]:
    mode = cfg["correctness"]["mode"]

    if mode == "stdout-marker":
        pass_pat = cfg["correctness"]["passPattern"]
        fail_pat = cfg["correctness"]["failPattern"]
        if pass_pat in run_stdout:
            return "PASS", "stdout contained pass marker"
        if fail_pat in run_stdout:
            return "FAIL", "stdout contained fail marker"
        return "UNKNOWN", "neither pass nor fail marker found in stdout"

    if mode == "output-diff":
        # accepts either a single "outputFile" (existing configs) or a list
        # under "outputFiles" (for benchmarks that write more than one
        # correctness-relevant file, e.g. cfd's density/momentum/density_energy)
        names = cfg["correctness"].get("outputFiles")
        if names is None:
            names = [cfg["correctness"]["outputFile"]]

        total_diffs = 0
        details = []
        for name in names:
            output_file = cfg["path"] / name
            golden_file = gpa_db_dir / f"pgo-baseline-output-{name}"
            if not output_file.exists():
                return "UNKNOWN", f"expected output file missing: {output_file}"
            if is_baseline:
                gpa_db_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(output_file, golden_file)
                continue
            if not golden_file.exists():
                return "UNKNOWN", "no golden reference captured yet — run 'baseline' first"
            current_lines = output_file.read_text().splitlines()
            golden_lines = golden_file.read_text().splitlines()
            if current_lines != golden_lines:
                diffs = sum(1 for a, b in zip(current_lines, golden_lines) if a != b)
                diffs += abs(len(current_lines) - len(golden_lines))
                total_diffs += diffs
                details.append(f"{name}: {diffs} differing/missing line(s)")

        if is_baseline:
            return "PASS", "baseline output captured as golden reference"
        if total_diffs == 0:
            return "PASS", "output matches golden reference exactly" if len(names) == 1 else f"all {len(names)} output files match golden reference exactly"
        return "FAIL", "; ".join(details) + " vs golden reference"

    if mode == "stdout-diff":
        # golden reference is stored as JSON (not newline-joined text) because
        # "\n".join(lines) does not round-trip symmetrically through .splitlines()
        # when `lines` has trailing empty strings -- that mismatch previously
        # produced a false-positive FAIL on output that was actually identical.
        ignore_patterns = cfg["correctness"].get("ignorePatterns", [])
        golden_file = gpa_db_dir / "pgo-baseline-stdout.json"
        current_lines = [
            line for line in run_stdout.splitlines()
            if not any(pat in line for pat in ignore_patterns)
        ]
        if is_baseline:
            gpa_db_dir.mkdir(parents=True, exist_ok=True)
            golden_file.write_text(json.dumps(current_lines))
            return "PASS", "baseline stdout captured as golden reference"
        if not golden_file.exists():
            return "UNKNOWN", "no golden reference captured yet — run 'baseline' first"
        golden_lines = json.loads(golden_file.read_text())
        if current_lines == golden_lines:
            return "PASS", "stdout (minus ignored/non-deterministic lines) matches golden reference exactly"
        diffs = sum(1 for a, b in zip(current_lines, golden_lines) if a != b)
        diffs += abs(len(current_lines) - len(golden_lines))
        return "FAIL", f"{diffs} differing/missing line(s) vs golden stdout reference"

    return "UNKNOWN", f"unrecognized correctness mode: {mode}"


def measure(benchmark: str, is_baseline: bool) -> dict:
    cfg = load_benchmark(benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"

    build(cfg)
    try:
        end_to_end_seconds, last_stdout = timed_runs(cfg)
    except BenchmarkTimeout as e:
        return {
            "benchmark": benchmark,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "endToEndSeconds": None,
            "kernelTimesNs": {},
            "localTimingNote": "not profiled — run timed out",
            "correctness": "FAIL",
            "correctnessDetail": str(e),
        }
    kernel_times_ns, local_timing_note = profile_kernels(cfg)
    correctness, correctness_detail = check_correctness(cfg, gpa_db_dir, is_baseline, last_stdout)

    return {
        "benchmark": benchmark,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endToEndSeconds": end_to_end_seconds,
        "kernelTimesNs": kernel_times_ns,
        "localTimingNote": local_timing_note,
        "correctness": correctness,
        "correctnessDetail": correctness_detail,
    }


def cmd_baseline(args):
    result = measure(args.benchmark, is_baseline=True)
    cfg = load_benchmark(args.benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"
    gpa_db_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = gpa_db_dir / "pgo-baseline.json"
    baseline_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"\nBaseline written to {baseline_path}", file=sys.stderr)


def cmd_compare(args):
    cfg = load_benchmark(args.benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"
    baseline_path = gpa_db_dir / "pgo-baseline.json"
    if not baseline_path.exists():
        sys.exit(f"No baseline found at {baseline_path} — run 'baseline' before editing source.")
    baseline = json.loads(baseline_path.read_text())

    current = measure(args.benchmark, is_baseline=False)

    ledger_path = gpa_db_dir / "pgo-ledger.md"
    if not ledger_path.exists():
        template = LEDGER_TEMPLATE.read_text().replace("<BENCHMARK>", args.benchmark)
        ledger_path.write_text(template)

    if current["endToEndSeconds"] is None:
        # timed out before any measurement was possible — nothing to compute speedup from
        result = {**current}
        print(json.dumps(result, indent=2))
        entry_lines = [
            f"\n### {result['timestamp']} — {args.benchmark}",
            f"- Correctness: {result['correctness']} ({result['correctnessDetail']})",
        ]
        with ledger_path.open("a") as f:
            f.write("\n".join(entry_lines) + "\n")
        print(f"\nLedger updated at {ledger_path} (facts only — agent must append its KEPT/REVERTED decision)", file=sys.stderr)
        return

    end_to_end_speedup = baseline["endToEndSeconds"] / current["endToEndSeconds"]
    kernel_speedups = {}
    for name, base_ns in baseline["kernelTimesNs"].items():
        cur_ns = current["kernelTimesNs"].get(name)
        if cur_ns:
            kernel_speedups[name] = base_ns / cur_ns

    result = {
        **current,
        "baselineEndToEndSeconds": baseline["endToEndSeconds"],
        "endToEndSpeedup": end_to_end_speedup,
        "kernelSpeedups": kernel_speedups,
    }
    print(json.dumps(result, indent=2))

    entry_lines = [
        f"\n### {result['timestamp']} — {args.benchmark}",
        f"- Correctness: {result['correctness']} ({result['correctnessDetail']})",
        f"- End-to-end: {baseline['endToEndSeconds']:.4f}s -> {current['endToEndSeconds']:.4f}s ({end_to_end_speedup:.3f}x)",
    ]
    if kernel_speedups:
        for name, speedup in kernel_speedups.items():
            entry_lines.append(
                f"- Kernel {name}: {baseline['kernelTimesNs'][name]:.0f}ns -> {current['kernelTimesNs'][name]:.0f}ns ({speedup:.3f}x)"
            )
    else:
        entry_lines.append(f"- Local kernel timing: unavailable ({current['localTimingNote']})")
    with ledger_path.open("a") as f:
        f.write("\n".join(entry_lines) + "\n")
    print(f"\nLedger updated at {ledger_path} (facts only — agent must append its KEPT/REVERTED decision)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_baseline = sub.add_parser("baseline", help="Capture pre-edit reference state")
    p_baseline.add_argument("benchmark")
    p_baseline.set_defaults(func=cmd_baseline)

    p_compare = sub.add_parser("compare", help="Re-measure after an edit and diff vs baseline")
    p_compare.add_argument("benchmark")
    p_compare.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
