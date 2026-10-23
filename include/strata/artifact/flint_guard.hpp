// Architecture guard for Recursive Flint GGUFs (general.architecture == "flint"), the Flint counterpart of
// Qwen4ExpGuard. Values are from the model card; metadata KEY NAMES are unverified until a GGUF header is dumped.
#pragma once
#include <cstdint>
#include <string>
#include "strata/artifact/gguf_reader.hpp"

namespace strata {
struct FlintSpec {
    uint32_t layers = 49, dense_layers = 2, moe_layers = 47;
    uint32_t swa_layers = 35, full_layers = 14, window = 513;     // current token + 512 before it
    uint32_t heads = 32, kv_heads = 4, head_dim = 128;
    uint32_t experts = 256, experts_used = 6, shared_experts = 1;
    uint32_t vocab = 150144, context = 262144;
    bool qk_norm = true, attn_sinks = true;
    bool rope_on_swa = true, rope_on_full = false;                // full-attention layers carry no positional encoding
};

inline std::string check_flint_architecture(const GgufFile& g, const FlintSpec& w = {}) {
    const MetaValue* arch = g.get("general.architecture");
    if (!arch) return "missing general.architecture";
    if (arch->s != "flint") return "architecture is '" + arch->s + "', this path requires 'flint'";
    const struct { const char* key; uint64_t want; } reqs[] = {
        {"flint.block_count", w.layers}, {"flint.attention.head_count", w.heads},
        {"flint.attention.head_count_kv", w.kv_heads}, {"flint.expert_count", w.experts},
        {"flint.expert_used_count", w.experts_used}};
    for (const auto& r : reqs) {                                  // TODO: confirm key names against a real header
        const MetaValue* v = g.get(r.key);
        if (!v) return std::string("missing ") + r.key;
        if (v->u != r.want) return std::string(r.key) + " = " + std::to_string(v->u) + ", expected " + std::to_string(r.want);
    }
    return {};
}
}  // namespace strata
