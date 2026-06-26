# ML-PGO -- GPU Performance Advisor (GPA), modernized for CUDA 12 / WSL2

This repo is a fork of [GPA (GPU Performance Advisor)](https://github.com/Jokeren/GPA)
by Keren Zhou, Xiaoming Meng, Rahul Sai, Dejan Grubisic, and John Mellor-Crummey
(Rice University). Licensed under the [BSD 3-Clause License](LICENSE).

**What this fork adds:** 13 compatibility patches to the HPCToolkit submodule
and supporting fixes to build and run GPA against a modern spack stack
(dyninst@master, boost@1.84, binutils@2.41, intel-xed current, libdwarf@0.10)
on CUDA 12 and WSL2. The original repo targets a 2018-era toolchain; this fork
makes it work on current hardware.

---

## Original project

> GPA is a performance advisor for NVIDIA GPUs that suggests potential code
> optimization opportunities at a hierarchy of levels, including individual lines,
> loops, and functions. GPA uses data flow analysis to approximately attribute
> measured instruction stalls to their root causes and estimates each
> optimization's speedup based on a PC sampling-based performance model.

Papers:
- K. Zhou et al., "An Automated Tool for Analysis and Tuning of GPU-accelerated Code in HPC Applications." *IEEE TPDS* (2021).
- K. Zhou et al., "GPA: A GPU Performance Advisor Based on Instruction Sampling." *CGO 2021*, doi: 10.1109/CGO51591.2021.9370339.

---

## Environment

Tested on:
- GPU: NVIDIA RTX 3070 (Ampere SM 8.6)
- OS: Ubuntu 22.04 on WSL2 (Windows 11, driver 610.43)
- CUDA: 12.0 (apt install)
- spack: current master

---

## Patches applied

All patches live on the `gpa-compat` branch of the hpctoolkit submodule
([anthonyramirez244/hpctoolkit](https://github.com/anthonyramirez244/hpctoolkit/tree/gpa-compat)).

### Build system (`configure` / `configure.ac`)
- Add `-lsframe` for modern binutils (2.41+) SFrame stack-trace metadata support
- Add `-lzstd` for modern binutils zstd-compressed debug sections
- Guard `-ldynDwarf`/`-ldynElf` -- these were merged into other dyninst libs and no longer ship as separate `.so` files

### Source fixes (GCC 13 / C++17 strictness)
- **`Metric-AExpr.hpp`**: unscoped `#define epsilon` clobbered `std::numeric_limits<T>::epsilon()`; rescoped to a file-local `static const double`
- **`Struct-Inline.hpp`**, **`StringTable.hpp`**: `operator()` on comparison functors must be `const`-qualified in C++17

### Dyninst API changes
- **`Struct.cpp`**: `findModuleByOffset` dropped the `set<Module*>` overload; switched to the `Module*` return form
- **`CudaBlock.hpp`**, **`CudaCFGFactory.hpp`**, **`CudaCodeSource.hpp`**, **`CudaFunction.hpp`**: `PARSER_EXPORT` renamed to `DYNINST_EXPORT`

### CUDA source build fixes
- **`AnalyzeInstruction.cpp`**, **`CudaBlock.cpp`**: missing `#include <dyn_regs.h>` for `Arch_cuda`
- **`CudaBlock.cpp`**: `NULL` byte pointer passed to `InstructionAPI::Instruction`; newer dyninst has no NULL guard -- replaced with a zeroed static array
- **`Instruction.hpp`**: off-by-end loop (`end_pos != npos` instead of `< s.size()`) could read past the string; also made `isalnum` check unsigned-char-safe

### intel-xed API removal
- **`x86ISAXed.cpp`**, **`x86-process-ranges.cpp`**, **`x86-unwind-support.c`**: `xed_operand_values_get_branch_displacement_int32` was removed; replaced with `_int64` + `(int)` cast at all 5 call sites (x86 branch displacements are inherently <= 32 bits, so the cast is safe)

### libdwarf API renames (libdwarf 0.10+)
- **`eh-frames.cpp`**: `dwarf_init` -> `dwarf_init_b`, `dwarf_fde_cie_list_dealloc` -> `dwarf_dealloc_fde_cie_list`, `DW_DLC_READ` -> `DW_GROUPNUMBER_ANY`

### Dyninst SymtabAPI removal
- **`hpcfnbounds/main.cpp`**: `getObjectType()`/`obj_Unknown` removed; replaced with `isExecutable() || isSharedLibrary() || isUnlinkedObjectFile()`

### hpcrun runtime bugs
- **`gpu-metrics.h`**: missing `typedef` on enum declaration was a latent multiple-definition bug silenced by GCC's old `-fcommon` default; breaks as a hard link error under GCC 13 `-fno-common`
- **`files.c`**: `vdso_hash_str[HASH_LENGTH * 2]` was one byte too short -- `sprintf` always null-terminates, and the buffer is also used as a `strcat` source; increased to `HASH_LENGTH * 2 + 1`
- **`nvidia.c`**: `cuptiActivitySetAttribute` (inside `cupti_device_buffer_config`) requires `cuptiSubscribe` to have already run on CUDA 12 / CUPTI_API_VERSION 18; reordered so buffer config happens after subscribe
- **`cuda-api.c`**: `dlopen("libcuda.so", ...)` loads the wrong library on WSL2 -- the unversioned name resolves to a bare-metal Linux driver `.so` that cannot drive a GPU under WSL2's paravirtualization. Changed to `"libcuda.so.1"` which resolves to the actual WSL2 GPU bridge (same soname real CUDA binaries use via `DT_NEEDED`).

### GPA advisor logic
- **`GPUInstruction.cpp`**: kernel detection used `GKER (sec)` to find kernel CCT nodes, but GKER is attributed to a libhpcrun parent node -- not to cubin instruction nodes -- so `gpu_kernels` was always empty. Switched to `GINS` (PC samples land directly at instruction nodes).
- **`GPUInstruction.cpp`**: used `GINS:STL_NONE` as instruction weight; this is 0 for every memory-bound kernel where all sampled warps are stalled, suppressing all advice output. Switched to `GINS:STL_ANY`.
- **`GPUAdvisor-Advise.cpp`**: guarded division by `GKER:COUNT` against zero.

### GPA-Benchmark
- **`rodinia/huffman/Makefile`**: added `-arch=sm_86`; without an explicit arch flag, nvcc generates PTX-only fat binaries and nvdisasm cannot parse them, causing hpcstruct to emit warnings for every function and hpcprof to segfault.

---

## Manual profiling pipeline

`bench.py` silently swallows all tool output. Use this pipeline directly:

```bash
GPA_ROOT="/path/to/ML-PGO"
export PATH="$GPA_ROOT/gpa/bin:$GPA_ROOT/gpa/hpctoolkit/bin:$PATH"
cd "$GPA_ROOT/GPA-Benchmark/rodinia/<benchmark>"

rm -rf gpa-measurements gpa-database <binary>.hpcstruct
hpcrun -e gpu=nvidia,pc -o gpa-measurements ./<binary> <args>
hpcstruct --gpu-size 100000 --gpucfg yes -j 4 gpa-measurements
hpcstruct --gpu-size 100000 -j 4 ./<binary> -o <binary>.hpcstruct
hpcprof --gpu-arch A100 -S <binary>.hpcstruct -o gpa-database gpa-measurements
cat gpa-database/gpa.advice
```

### Example: Huffman (BFS is identical, substitute `./bfs` and graph input)

```bash
cd "$GPA_ROOT/GPA-Benchmark/rodinia/huffman"
rm -rf gpa-measurements gpa-database pavle.hpcstruct
hpcrun -e gpu=nvidia,pc -o gpa-measurements ./pavle \
  "$GPA_ROOT/GPA-Benchmark/data/huffman/test1024_H2.206587175259.in"
hpcstruct --gpu-size 100000 --gpucfg yes -j 4 gpa-measurements
hpcstruct --gpu-size 100000 -j 4 ./pavle -o pavle.hpcstruct
hpcprof --gpu-arch A100 -S pavle.hpcstruct -o gpa-database gpa-measurements
cat gpa-database/gpa.advice
```

---

## Sample results

### BFS (`Kernel`)
| Optimizer | Ratio | Speedup estimate |
|---|---|---|
| `GPUGlobalMemoryCoalesceOptimizer` | 53.9% | 2.17x |
| `GPUCodeReorderOptimizer` | 84.5% | 1.17x |

### Huffman (`vlc_encode_kernel_sm64huff`)
| Optimizer | Ratio | Speedup estimate |
|---|---|---|
| `GPUWarpBalanceOptimizer` | 44.4% | 1.80x |
| `GPUCodeReorderOptimizer` | 14.4% | 1.17x |
| `GPUDivergeReductionOptimizer` | 8.4% | 1.09x |
