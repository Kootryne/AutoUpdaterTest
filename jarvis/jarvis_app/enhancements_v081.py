from __future__ import annotations

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .github_auth import apply_patches as apply_github_auth
    from .capability_flow import apply_patches as apply_capability_flow

    apply_github_auth()
    apply_capability_flow()
    _PATCHED = True
