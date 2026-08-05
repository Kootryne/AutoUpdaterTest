from __future__ import annotations

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from .reliability_v083 import apply_patches as apply_reliability_v083
    apply_reliability_v083()
    _PATCHED = True
