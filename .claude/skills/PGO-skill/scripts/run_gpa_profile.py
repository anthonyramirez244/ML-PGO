#!/usr/bin/env python3
"""Run the GPA profiling pipeline (hpcrun -> hpcstruct -> hpcprof) for a benchmark
and (re)produce <benchmark-dir>/gpa-database/gpa.advice.

This replaces the manual, multi-command pipeline previously run by hand for
every benchmark/iteration (see CLAUDE.md's now-obsolete "manual pipeline"
block). Every subprocess streams straight to the terminal -- never captured
and hidden -- the same class of visibility bug that made bench.py's swallowed
stdout/stderr useless for debugging hpcrun/hpcstruct/hpcprof failures.

Usage: run_gpa_profile.py <benchmark>
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GPA_ROOT = SKILL_DIR.parents[2]  # .claude/skills/PGO-skill -> .claude/skills -> .claude -> GPA
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"

HPCTOOLKIT_BIN = GPA_ROOT / "gpa" / "hpctoolkit" / "bin"
GPA_BIN = GPA_ROOT / "gpa" / "bin"

DEFAULT_GPU_ARCH = "A100"  # closest hpcprof --gpu-arch model to this box's Ampere RTX 3070
DEFAULT_RUN_TIMEOUT_SECONDS = 60  # same default pgo_bench.py uses for a plain run
MIN_PROFILE_RUN_TIMEOUT_SECONDS = 300  # hpcrun instrumentation adds real overhead
                                        # over a bare run, so scale the plain-run
                                        # timeout up rather than reusing it directly
STRUCT_PROF_TIMEOUT_SECONDS = 900  # hpcstruct/hpcprof can be slow on larger binaries
                                    # (observed first-hand on heartwall -- slow, not
                                    # hung) but must still be bounded: a real hang
                                    # here (e.g. the boost double-load recursive-mutex
                                    # deadlock found earlier in this project) should
                                    # not block the skill forever.


class PipelineError(Exception):
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


def run(cmd: list, cwd: Path, env: dict, timeout: float | None = None) -> None:
    print(f"+ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PipelineError(
            f"'{cmd[0]}' did not finish within {timeout}s -- likely hung "
            f"(see STRUCT_PROF_TIMEOUT_SECONDS / MIN_PROFILE_RUN_TIMEOUT_SECONDS comments)"
        )
    if result.returncode != 0:
        raise PipelineError(f"'{cmd[0]}' exited {result.returncode}")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: run_gpa_profile.py <benchmark>")
    name = sys.argv[1]
    cfg = load_benchmark(name)
    path = cfg["path"]
    binary = cfg["runCmd"][0].removeprefix("./")
    args = cfg["runCmd"][1:]
    gpu_arch = cfg.get("gpuArch", DEFAULT_GPU_ARCH)
    run_timeout = max(
        cfg.get("runTimeoutSeconds", DEFAULT_RUN_TIMEOUT_SECONDS) * 8,
        MIN_PROFILE_RUN_TIMEOUT_SECONDS,
    )

    env = os.environ.copy()
    env["PATH"] = f"{GPA_BIN}:{HPCTOOLKIT_BIN}:{env['PATH']}"

    print(f"Building {cfg['dir']}...", file=sys.stderr)
    build_result = subprocess.run(cfg["buildCmd"], cwd=path, capture_output=True, text=True)
    if build_result.returncode != 0:
        sys.exit(f"Build failed for {cfg['dir']}:\n{build_result.stdout}\n{build_result.stderr}")

    measurements_dir = path / "gpa-measurements"
    hpcstruct_file = path / f"{binary}.hpcstruct"
    gpa_db_dir = path / "gpa-database"
    tmp_db_dir = path / "gpa-database-tmp"

    # Only clear the pipeline's own intermediates -- gpa-database also holds
    # pgo_bench.py's pgo-baseline.json / pgo-ledger.md / pgo-baseline-output-*,
    # which must survive a re-profile. hpcprof refuses to write into a db dir
    # that already exists, so it targets a scratch dir that gets merged in after.
    shutil.rmtree(measurements_dir, ignore_errors=True)
    hpcstruct_file.unlink(missing_ok=True)
    shutil.rmtree(tmp_db_dir, ignore_errors=True)

    try:
        run(
            ["hpcrun", "-e", "gpu=nvidia,pc", "-o", str(measurements_dir), f"./{binary}", *args],
            cwd=path, env=env, timeout=run_timeout,
        )
        run(
            ["hpcstruct", "--gpu-size", "100000", "--gpucfg", "yes", "-j", "4", str(measurements_dir)],
            cwd=path, env=env, timeout=STRUCT_PROF_TIMEOUT_SECONDS,
        )
        run(
            ["hpcstruct", "--gpu-size", "100000", "-j", "4", f"./{binary}", "-o", str(hpcstruct_file)],
            cwd=path, env=env, timeout=STRUCT_PROF_TIMEOUT_SECONDS,
        )
        run(
            ["hpcprof", "--gpu-arch", gpu_arch, "-S", str(hpcstruct_file), "-o", str(tmp_db_dir), str(measurements_dir)],
            cwd=path, env=env, timeout=STRUCT_PROF_TIMEOUT_SECONDS,
        )
    except PipelineError as e:
        sys.exit(f"GPA pipeline failed for {name}: {e}")

    advice_src = tmp_db_dir / "gpa.advice"
    if not advice_src.exists():
        sys.exit(f"hpcprof ran but produced no gpa.advice at {advice_src}")

    gpa_db_dir.mkdir(exist_ok=True)
    for item in tmp_db_dir.iterdir():
        dest = gpa_db_dir / item.name
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        shutil.move(str(item), str(dest))
    shutil.rmtree(tmp_db_dir, ignore_errors=True)

    print(f"\ngpa.advice refreshed at {gpa_db_dir / 'gpa.advice'}", file=sys.stderr)


if __name__ == "__main__":
    main()
