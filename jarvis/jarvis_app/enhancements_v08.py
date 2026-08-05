from __future__ import annotations

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .language_mode import apply_patches as apply_language
    from .github_skill_patches import apply_patches as apply_github_skills
    from .update_consent import apply_patches as apply_update_consent

    apply_language()
    apply_github_skills()
    apply_update_consent()
    _PATCHED = True
