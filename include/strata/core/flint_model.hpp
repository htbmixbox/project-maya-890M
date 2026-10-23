// Flint engine path - NOT IMPLEMENTED. Per decoder layer the engine must run:
//   attn_norm -> QKV (32q/4kv x 128) -> per-head QK RMSNorm -> RoPE (SWA layers only; full layers: none)
//   -> attention with learned per-head sink logit; SWA layers use a 513-token window (ring KV), 14 full layers a full KV
//   -> out proj -> residual -> ffn_norm -> layers 0-1: dense MLP; layers 2-48: router (256 experts, top-6) + 1 shared expert.
// Still needed before this can run (needs a GGUF header dump or Recursive's reference code, github.com/recursive-oss/rec):
//   [ ] tensor names + metadata keys   [ ] SWA/full layer pattern   [ ] router scoring (softmax/sigmoid, norm_topk, bias)
//   [ ] expert activation               [ ] RoPE theta/dims         [ ] sink tensor shape   [ ] chat template/tokenizer pack
//   [ ] pack builder (tools/iq_pack.py flint profile)   [ ] CUDA/HIP kernels (SWA+sink attention, 256-expert MoE)
#pragma once
#include "strata/artifact/flint_guard.hpp"
namespace strata {
struct FlintModel { static constexpr bool kImplemented = false; };
}  // namespace strata
