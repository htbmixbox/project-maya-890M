# Strix Point (Ryzen AI 300, Radeon 890M / 880M, gfx1150): experimental

Maya's setup and HIP build accept the Ryzen AI 300 "Strix Point" APUs (Radeon 890M / 880M, RDNA3.5, `gfx1150`) as an
**experimental** target. Nobody on the Maya side has measured it on a real chip: this page records what was changed
and what is still a guess. Please report what you see (last section).

Strix Point is a small sibling of Strix Halo: the same RDNA3.5 wave32 / 64 KiB LDS / signed-dot4 family, one pool of
system memory shared by the CPU and the GPU, a 16-CU iGPU.

## What setup and the build do for it

- **Detection:** `./setup.sh --check` lists the iGPU from the kernel's KFD topology as `gfx1150` and accepts it
  (`AMD_ARCHS` in `setup.py`); other integrated Radeons (gfx1152 Krackan, gfx1103 Phoenix, gfx1036) stay unsupported.
- **Build:** `maya.py` compiles one binary for `gfx1100;gfx1201;gfx1151;gfx1150`. `gfx1150` is in the "unvalidated" list
  of `cmake/hip_backend.cmake`, so CMake builds it with a warning. The HIP intrinsics already cover `__gfx1150__`
  (`include/strata/hip_compat/intrinsics.hpp`).
- **What it does NOT get:** no hipBLASLt tuning table exists for gfx1150 (`tools/hip/` has gfx1100 / gfx1151 / gfx1201),
  so the prompt's dense products use plain hipBLAS. Setup also has no unified-memory sizing for it: the GPU's "VRAM" it
  reports is only the BIOS carve-out, and the HIP config keeps its discrete-card defaults
  (`STRATA_GLM_RESERVE_MB=3072`, `STRATA_GLM_RAM_HEADROOM_GB=16`). Change them with `--env KEY=VALUE` if the engine
  starts too little or too much cache.

## Build by hand (Linux)

```sh
cmake -S . -B build-hip -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_HIP=ON -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_PREFILL_MMQ=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx1150
cmake --build build-hip --target strata -j 8
```

You need ROCm 7 (system `/opt/rocm` with hipcc and hipBLAS, or `ROCM_PATH`) whose compiler supports `gfx1150`.

## What to expect, and the open questions

- **Speed:** the 890M shares the system memory's bandwidth with the CPU and has 16 CUs; expect far lower numbers than
  the discrete cards. No measurement exists here.
- **Registers:** the 890M is reported to have a smaller vector register file per SIMD than gfx1151, so kernels tuned
  for other cards may spill or run fewer blocks per CU. That costs speed, not correctness, and is unmeasured.
- **Memory:** RAM decides the model. Give the GPU room with a small BIOS carve-out and, if needed, the kernel's
  `ttm.pages_limit` / `ttm.page_pool_size`; setup changes no host setting.
- **Windows:** the HIP backend is Linux only.

## Reports

The machine (CPU, RAM, BIOS carve-out), the kernel and ROCm versions, the model, the engine log and the output of
`./setup.sh --check`.
