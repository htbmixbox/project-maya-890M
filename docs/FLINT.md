# Flint (Recursive, unsloth/flint-GGUF)
`./maya.sh --setup --model Flint-UD-Q4_K_XL` downloads one of: UD-IQ1_M, UD-IQ2_XXS, UD-Q2_K_XL, UD-IQ3_XXS,
UD-Q3_K_XL, UD-Q4_K_XL, UD-Q5_K_XL, UD-Q6_K_XL, Q8_0, BF16 (3 shards). Downloads are not sha256-checked (hashes not
published in the patch). The engine step then stops with a message: the Flint compute path is a skeleton, see the
checklist in include/strata/core/flint_model.hpp.
