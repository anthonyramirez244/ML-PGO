---
name: PGO-skill
description: Analyzes GPA (GPU Performance Advisor) gpa.advice profiler output, locates the responsible CUDA/PyTorch source, proposes and applies one targeted optimization, verifies correctness, and benchmarks local (kernel) and end-to-end speedup. Use when asked to optimize a CUDA kernel, analyze gpa.advice output, investigate a GPU performance regression, or sweep the optimization loop across every configured benchmark in one session.
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
python3 ${CLAUDE_SKILL_DIR}/scripts/get_profile_summary.py <benchmark> --top 5
```

This is a cached wrapper around `parse_advice.py`, keyed by `gpa.advice`'s content hash — on an
unchanged profile it returns the same JSON without re-parsing 1500-4000 lines of raw advice text,
and it persists the result to `<benchmark-dir>/gpa-database/profile-summary.json`. The cache is
purely an accelerant: if the script is ever unavailable, run `parse_advice.py
<benchmark-dir>/gpa-database/gpa.advice --top 5` directly instead — same output, just uncached.

If the output format needs interpreting (optimizer meanings, how `Hot BLAME` locations map to
source lines, the duplicate-kernel-block quirk), read
[references/gpa-advice-format.md](references/gpa-advice-format.md) — don't re-derive it from raw
`gpa.advice` text.

### 3. Check the ledger before proposing anything

```
python3 ${CLAUDE_SKILL_DIR}/scripts/build_ledger_index.py <benchmark>
```

Then `Read` the resulting `<benchmark-dir>/gpa-database/pgo-ledger-index.json` — an O(1)
kernel+optimizer -> KEPT/REVERTED lookup derived from `pgo-ledger.md`, instead of reading the full
ledger text every invocation. Skip any finding whose kernel+optimizer pair is already indexed as
`REVERTED` — don't re-attempt something already known not to work, and don't re-derive that
decision by scanning conversation history. `pgo-ledger.md` remains the source of truth; the index
is always rebuilt from it and never hand-edited. If a ledger entry can't be attributed to a known
kernel+optimizer pair (the script logs this to stderr when it happens), or if the index script is
ever unavailable, fall back to reading `pgo-ledger.md` directly rather than skipping the check.

### 3.5. Consult the cross-benchmark prior

```
python3 ${CLAUDE_SKILL_DIR}/scripts/build_global_index.py
python3 ${CLAUDE_SKILL_DIR}/scripts/get_optimizer_prior.py <benchmark>
```

The first command aggregates all benchmarks' ledger indexes (step 3's per-benchmark view) into
one cross-benchmark view keyed by optimizer, at `state/global-optimizer-index.json` -- "has
`GPULoopUnrollOptimizer` worked on any *other* benchmark?", not just this one. It's rebuilt from
the tracked `pgo-ledger.md` files every time and skips the write when nothing changed, so running
it is always safe and cheap. The second command joins that against only the optimizers named by
this benchmark's current top findings and prints a small `priorElsewhere` annotation per
candidate (`keptCount`/`revertedCount` on *other* benchmarks) -- deliberately excluding this
benchmark's own history, which step 3 already covers.

Use this to **rank**, not to filter: among findings not already REVERTED on this benchmark (step
3), prefer one whose optimizer has a strong track record elsewhere. A poor track record elsewhere
is a reason for caution, not automatic exclusion -- every benchmark's kernel is different, and an
optimizer that failed once elsewhere can still be the right fix here.

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
top of unknown existing changes. **Exception:** on a benchmark's very first-ever attempt right
after onboarding, `??` (untracked) is expected if the vendored source hasn't been committed yet —
that's your own just-added file, not unknown prior work, so it's safe to proceed. But note this
for step 8: `git checkout -- <file>` only works on a *tracked* file, and does nothing (silently
errors) on one that's still untracked. Commit the untouched vendored source in its own commit
*before* attempting the first optimization on a new benchmark, so step 8's revert path always has
something real to check out against — if you skip that and end up REVERTED on an untracked file
anyway, restore it by hand (re-copy from the original source, or manually undo the exact edit) and
verify the result matches the pre-edit content before moving on. Otherwise make the change with
`Edit`.

### 7. Verify correctness and benchmark

```
python3 ${CLAUDE_SKILL_DIR}/scripts/pgo_bench.py compare <benchmark>
```

This rebuilds, re-runs, re-profiles, checks correctness against the baseline, prints
end-to-end and per-kernel speedups, and appends a **facts-only** entry to
`<benchmark-dir>/gpa-database/pgo-ledger.md` (correctness verdict + measured speedups — no
judgment call).

**Local (per-kernel) timing via nsys is best-effort, and on WSL2 it's usually just broken.**
`nsys`'s CUPTI Activity kernel-trace capture has a confirmed, still-unresolved gap under WSL2 (NVIDIA
staff acknowledged this on the developer forums; it slipped several planned releases and is still
reported broken as of early 2026) — it can fail cleanly (empty `kernelTimesNs`, a `localTimingNote`
explaining why) or hang until its own 180s timeout, handled the same way either way. If
`kernelSpeedups` comes back empty, do not invent a kernel-level number — fall back to end-to-end,
citing `localTimingNote`, and check the next paragraph first.

**Prefer `selfReportedKernelSpeedups` over nsys when a benchmark has one.** Some benchmarks already
wrap their own kernel launch with `chrono`/`cudaEvent`/`gettimeofday` and print the result — that
number has nothing to do with nsys and isn't subject to the WSL2 gap at all. `benchmarks.json`'s
optional `localTiming` list (`{label, regex, unit, occurrence?}` entries) tells `pgo_bench.py` how to
parse it into `selfReportedKernelTimesNs`/`selfReportedKernelSpeedups`, computed and logged
alongside `kernelSpeedups` whenever present. Currently configured for huffman, gaussian, lud, srad,
b+tree, and xsbench — confirmed working end-to-end 2026-07-19 (b+tree's two kernels share an
identical printed label, disambiguated via `occurrence: 1`/`2`, in call order). bfs, hotspot, kmeans,
and nw have no self-reported timer in their source (dead/commented-out instrumentation or none at
all) — for those four, end-to-end remains the only signal unless instrumentation is added. A
benchmark's `localTiming` labels aren't always 1:1 with individual kernel names — some (e.g.
gaussian's `Fan1+Fan2 (combined)`) report a total across multiple kernel launches, or (lud's
`lud_cuda (kernel+memcpy)`) include memory-transfer time alongside the kernel — always read the
label, don't assume it isolates exactly one kernel's own time.

### 8. Apply policy, then log your decision

**End-to-end alone is not reliable enough to govern this decision.** Confirmed in both directions
on 2026-07-19: xsbench's end-to-end showed a 0.951x "regression" that `selfReportedKernelSpeedups`
proved was actually a 1.000x no-op (nvcc silently declined to unroll a runtime-variable-trip-count
loop); gaussian's end-to-end showed a 1.018x "improvement" that `selfReportedKernelSpeedups` proved
was actually a 0.739x kernel-level regression (a redundant-load fix that likely cost more in
register pressure than it saved). Trusting end-to-end alone would have gotten *both* of those
backwards. Determine the **governing speedup** with this precedence, per kernel/label you actually
edited:

1. `selfReportedKernelSpeedups` entry for it, if present.
2. Else `kernelSpeedups` (nsys) entry for it, if present.
3. Else `endToEndSpeedup` — and say so explicitly in the ledger (`no kernel-level signal available
   — decision based on end-to-end only`), since this is the weakest signal, not the default one.

If the matching label is a combined/impure metric (step 7's caveat — e.g. gaussian's
`Fan1+Fan2 (combined)`, lud's `lud_cuda (kernel+memcpy)`), it still outranks end-to-end, but name it
as combined/impure in the ledger so a future reader doesn't mistake it for a clean single-kernel
number.

- If correctness is **FAIL**: revert immediately with `git checkout -- <file>`, then append a line
  to the ledger: `- Decision: REVERTED — correctness failed (<detail>)`. Stop and report. Do not
  retry the same change.
- If correctness is **PASS** but the governing speedup is **< 1.0** (a regression): revert the same
  way, log `- Decision: REVERTED — regression (Nx <governing metric name>)`, even if
  `endToEndSpeedup` alone would have looked like an improvement (see gaussian's case above). Stop
  and report.
- If correctness is **PASS** and the governing speedup is real: keep the change, log
  `- Decision: KEPT — <governing speedup, plus the other available numbers for context>`.
- One optimization attempt per invocation unless the user explicitly asks you to continue to the
  next finding. If continuing to the next finding, go back to step 0 — the profile must be
  regenerated against the code as it now stands before picking the next target.
- Never edit a file outside the target benchmark's directory.
- Never skip step 7 to report a speedup number — only report numbers a script actually printed.

### 9. Report

Summarize for the user: what changed (file:line, one-sentence why), the correctness verdict, which
speedup governed the decision and why (step 8's precedence), the other available numbers for
context, and the decision you logged.

## Sweep mode

When asked to run the optimization loop across multiple benchmarks (or "all" of them) in one
sitting, loop over `assets/benchmarks.json`'s entries in order. For each benchmark:

1. **Check for a cheap exhaustion verdict first, without re-profiling.** If
   `<benchmark-dir>/gpa-database/profile-summary.json` exists, run steps 3 (ledger index) and 3.5
   against its *already-cached* findings only -- do not regenerate `gpa.advice` for this check. If
   every one of those cached top findings is already `REVERTED` on this benchmark, this benchmark
   is exhausted: skip steps 0-8 entirely, run
   `python3 ${CLAUDE_SKILL_DIR}/scripts/log_sweep_result.py <benchmark> --status exhausted`, and
   move to the next benchmark. (This verdict is only as fresh as the cached profile-summary.json --
   if the benchmark's source has changed since it was generated, delete that file to force a real
   recheck on the next sweep.)
2. **Otherwise, run the full loop (steps 0-8) normally** for this benchmark, including the fresh
   profile regeneration step 0 always requires. The Apply step (6) still goes through normal Edit
   tool approval -- sweep mode does not skip or batch that confirmation, it only automates the
   deterministic steps around it.
3. **After step 8's decision is logged to `pgo-ledger.md`**, also record it in the cross-sweep
   audit trail:
   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/log_sweep_result.py <benchmark> --status attempted \
       --kernel <kernel> --optimizer <optimizer> --decision <KEPT|REVERTED> \
       --e2e-speedup <value if PASS, omit if not>
   ```
4. Continue to the next benchmark. One optimization attempt per benchmark per sweep, same as the
   single-benchmark policy in step 8.

At the end, summarize the whole sweep from `state/sweep-log.jsonl` (a `tail -n <count>` of the
lines just appended) rather than re-reading every benchmark's `pgo-ledger.md` -- that file exists
specifically so a multi-benchmark run is reviewable in one small pass.

## Files in this skill

- `scripts/run_gpa_profile.py` — runs the full `hpcrun -> hpcstruct -> hpcprof` pipeline for a
  benchmark and (re)writes `gpa-database/gpa.advice`, merging its output into `gpa-database/`
  without touching the ledger/baseline files that also live there. Each subprocess's full output
  is captured to `gpa-database/pipeline-logs/<n>-<step>.log`; only a one-line pass/fail per step
  (plus the log tail on failure) is printed.
- `scripts/parse_advice.py` — parses/dedupes/ranks `gpa.advice` into JSON (see
  [references/gpa-advice-format.md](references/gpa-advice-format.md) for the format it reads).
- `scripts/get_profile_summary.py` — cached wrapper around `parse_advice.py`, keyed by
  `gpa.advice`'s content hash; persists to `gpa-database/profile-summary.json`. Optional
  accelerant, not a hard dependency — falls back cleanly to calling `parse_advice.py` directly.
- `scripts/build_ledger_index.py` — derives `gpa-database/pgo-ledger-index.json` (a
  kernel+optimizer -> KEPT/REVERTED lookup) from `pgo-ledger.md`, keyed by the ledger's content
  hash. Optional accelerant, not a hard dependency — `pgo-ledger.md` remains the source of truth.
- `scripts/pgo_bench.py` — builds, runs (median of N for end-to-end wall-clock), profiles with
  `nsys` for per-kernel time (best-effort — see the WSL2 caveat in step 7), checks correctness
  (stdout marker or output-file diff, per `assets/benchmarks.json`), and computes speedups.
  `baseline` captures reference state; `compare` measures after a change and updates the ledger.
  Also parses `assets/benchmarks.json`'s optional `localTiming` entries out of the benchmark's own
  stdout (`parse_local_timing()`) into `selfReportedKernelTimesNs`/`selfReportedKernelSpeedups` —
  an nsys-independent kernel-timing source for benchmarks that self-report one.
- `scripts/build_global_index.py` — aggregates every benchmark's ledger data into one
  cross-benchmark view keyed by optimizer, at `state/global-optimizer-index.json`. Always fully
  rebuilt from the tracked `pgo-ledger.md` files (never hand-edited); write is skipped when the
  combined content hash is unchanged.
- `scripts/get_optimizer_prior.py` — joins `state/global-optimizer-index.json` against a single
  benchmark's current top findings, returning a small `priorElsewhere` annotation per candidate
  (has this optimizer worked on *other* benchmarks?). Depends on both `profile-summary.json` and
  `state/global-optimizer-index.json` already existing.
- `scripts/log_sweep_result.py` — appends one compact line to `state/sweep-log.jsonl` per
  benchmark processed during a sweep (see "Sweep mode" above). Pure append, never rewritten —
  `pgo-ledger.md` remains the source of truth for the decision itself.
- `assets/benchmarks.json` — per-benchmark config: directory, build/run commands, correctness
  check mode, target kernel names.
- `assets/ledger-template.md` — header used when a benchmark's ledger file is created for the
  first time.
- `state/global-optimizer-index.json` — generated, gitignored (see `state/.gitignore`); rebuild
  any time with `build_global_index.py`.
- `state/sweep-log.jsonl` — generated but tracked in git (not regenerable — see
  `state/.gitignore`); the durable audit trail of every sweep run.
