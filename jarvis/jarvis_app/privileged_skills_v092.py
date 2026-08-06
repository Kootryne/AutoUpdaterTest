from __future__ import annotations

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .skill_builder_v092 import patch_builder_and_runtime
    from .skill_approval_v092 import patch_skill_approval_flow

    patch_builder_and_runtime()
    patch_skill_approval_flow()
    _PATCHED = True
