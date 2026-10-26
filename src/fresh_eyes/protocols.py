"""Frozen comparison protocols, shared by execution and result validation."""
PROTOCOLS = {
    "A": {"max_patch_rounds": 0, "route": None},
    "B": {"max_patch_rounds": 1, "route": "nano_ultra"},
    "C": {"max_patch_rounds": 2, "route": "nano_ultra"},
    "D": {"max_patch_rounds": 2, "route": "all_ultra"},
}
