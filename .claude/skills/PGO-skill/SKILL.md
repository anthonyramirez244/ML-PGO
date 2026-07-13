---
name: PGO-skill
description: Analyzes GPA (GPU Performance Advisor) gpa.advice profiler output, locates the responsible CUDA/PyTorch source, proposes and applies one targeted optimization, verifies correctness, and benchmarks local (kernel) and end-to-end speedup. Use when asked to optimize a CUDA kernel, analyze gpa.advice output, or investigate a GPU performance regression in this repo.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# PGO-skill

You guide a full profile-guided-optimization loop for a GPA-profiled CUDA benchmark in this repo:
understand the profile, locate and analyze the responsible code, suggest and apply one fix,
verify correctness, and measure real local (kernel) and end-to-end speedup. Do not skip steps
and do not invent numbers — every claim must trace back to something you actually read or a
number a script actually printed.

The benchmark name (e.g. `bfs`, `huffman`) and its config live in
`${CLAUDE_SKILL_DIR}/assets/benchmarks.json`. If the user hasn't named a benchmark, ask which one.

## Workflow

### 0. Generate a fresh GPU profile

Always regenerate `gpa.advice` at the start of every invocation — the pipeline builds current
source first, so if a previous run's optimization was KEPT, this reflects that change rather than
acting on stale advice from before it:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/run_gpa_profile.py <benchmark>
```

This builds the benchmark, then runs `hpcrun -> hpcstruct -> hpcprof` and writes
`<benchmark-dir>/gpa-database/gpa.advice`. It only clears the pipeline's own intermediates
(`gpa-measurements`, `<binary>.hpcstruct`) — it never touches `pgo-baseline.json`,
`pgo-ledger.md`, or the `pgo-baseline-output-*` files that live in the same `gpa-database/`
directory. Do not run the old manual `hpcrun`/`hpcstruct`/`hpcprof` command sequence by hand —
this script replaces it (and fixes a real hazard the manual version had: a bare `rm -rf
gpa-database` would have destroyed the ledger and baseline alongside the pipeline's own output).

### 1. Capture a baseline (only if one doesn't already exist)

Check for `<benchmark-dir>/gpa-database/pgo-baseline.json`. If missing, run this *before* touching
any source — it is the reference every later comparison is measured against:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/pgo_bench.py baseline <benchmark>
```

### 2. Understand the profile

```
python3 ${CLAUDE_SKILL_DIR}/scripts/parse_advice.py <benchmark-dir>/gpa-database/gpa.advice --top 5
```

If the output format needs interpreting (optimizer meanings, how `Hot BLAME` locations map to
source lines, the duplicate-kernel-block quirk), read
[references/gpa-advice-format.md](references/gpa-advice-format.md) — don't re-derive it from raw
`gpa.advice` text.

### 3. Check the ledger before proposing anything

If `<benchmark-dir>/gpa-database/pgo-ledger.md` exists, read it. Skip any finding already logged
there as `REVERTED` for the same kernel + optimizer — don't re-attempt something already known not
to work, and don't re-derive that decision by scanning conversation history. The ledger is the
source of truth for what's already been tried.

### 4. Locate and analyze

`Read` the actual source at every `location` cited by the top (not-yet-reverted) finding. Ground
your analysis in the real code — never describe a fix for code you haven't read.

### 5. Suggest

State the specific change: what line(s), what transformation, and why it addresses the specific
stall reason (`GINS:LAT_*` etc.) the finding cited.

### 6. Apply

Before editing, confirm the target file is clean:

```
git status --porcelain -- <file>
```

If it's not empty (uncommitted changes already present), STOP and tell the user — do not edit on
top of unknown existing changes. Otherwise make the change with `Edit`.

### 7. Verify correctness and benchmark

```
python3 ${CLAUDE_SKILL_DIR}/scripts/pgo_bench.py compare <benchmark>
```

This rebuilds, re-runs, re-profiles, checks correctness against the baseline, prints
end-to-end and per-kernel speedups, and appends a **facts-only** entry to
`<benchmark-dir>/gpa-database/pgo-ledger.md` (correctness verdict + measured speedups — no
judgment call).

**Local (per-kernel) timing is best-effort.** It depends on `nsys` successfully capturing GPU
kernel activity, which is not guaranteed on every platform (a known limitation on some WSL2/driver
combinations — see `localTimingNote` in the output). If `kernelSpeedups` comes back empty, do not
invent a kernel-level number — report end-to-end speedup only and say local timing was
unavailable, citing `localTimingNote`.

### 8. Apply policy, then log your decision

- If correctness is **FAIL**: revert immediately with `git checkout -- <file>`, then append a line
  to the ledger: `- Decision: REVERTED — correctness failed (<detail>)`. Stop and report. Do not
  retry the same change.
- If correctness is **PASS** but `endToEndSpeedup < 1.0` (a regression): revert the same way, log
  `- Decision: REVERTED — regression (Nx end-to-end)`. Stop and report.
- If correctness is **PASS** and speedup is real: keep the change, log
  `- Decision: KEPT — <end-to-end and kernel speedup numbers>`.
- One optimization attempt per invocation unless the user explicitly asks you to continue to the
  next finding. If continuing to the next finding, go back to step 0 — the profile must be
  regenerated against the code as it now stands before picking the next target.
- Never edit a file outside the target benchmark's directory.
- Never skip step 7 to report a speedup number — only report numbers a script actually printed.

### 9. Report

Summarize for the user: what changed (file:line, one-sentence why), the correctness verdict, the
measured local (kernel) speedup and end-to-end speedup, and the decision you logged.

## Files in this skill

- `scripts/run_gpa_profile.py` — runs the full `hpcrun -> hpcstruct -> hpcprof` pipeline for a
  benchmark and (re)writes `gpa-database/gpa.advice`, merging its output into `gpa-database/`
  without touching the ledger/baseline files that also live there.
- `scripts/parse_advice.py` — parses/dedupes/ranks `gpa.advice` into JSON (see
  [references/gpa-advice-format.md](references/gpa-advice-format.md) for the format it reads).
- `scripts/pgo_bench.py` — builds, runs (median of N for end-to-end wall-clock), profiles with
  `nsys` for per-kernel time, checks correctness (stdout marker or output-file diff, per
  `assets/benchmarks.json`), and computes speedups. `baseline` captures reference state;
  `compare` measures after a change and updates the ledger.
- `assets/benchmarks.json` — per-benchmark config: directory, build/run commands, correctness
  check mode, target kernel names.
- `assets/ledger-template.md` — header used when a benchmark's ledger file is created for the
  first time.
