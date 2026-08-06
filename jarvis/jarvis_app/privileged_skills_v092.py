from __future__ import annotations

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from . import capability_flow
    from .skill_builder_v092 import patch_builder_and_runtime
    from .skill_approval_v092 import patch_skill_approval_flow
    from .skills import SkillSystem

    patch_builder_and_runtime()
    patch_skill_approval_flow()

    prior_prepare = SkillSystem.prepare_privileged_build

    def prepare_without_stale_confirmation(self: SkillSystem, **kwargs):
        capability_flow._clear_pending()
        return prior_prepare(self, **kwargs)

    SkillSystem.prepare_privileged_build = prepare_without_stale_confirmation
    SkillSystem.start_build = prepare_without_stale_confirmation

    prior_handles = SkillSystem.handles_tool

    def handles_every_published_skill_tool(self: SkillSystem, name: str) -> bool:
        if prior_handles(self, name):
            return True
        return any(schema.get("name") == name for schema in self.schemas())

    SkillSystem.handles_tool = handles_every_published_skill_tool
    _PATCHED = True
