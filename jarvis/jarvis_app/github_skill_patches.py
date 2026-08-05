from __future__ import annotations

from typing import Any

from .shared_skill_manager import SharedSkillManager, read_disabled

_PATCHED = False

def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .skill_builder import SkillBuilder
    from .skill_schema import SkillRegistry
    from .skills import SkillSystem

    original_registry_reload = SkillRegistry.reload
    original_system_init = SkillSystem.__init__
    original_system_shutdown = SkillSystem.shutdown
    original_schemas = SkillSystem.schemas
    original_call = SkillSystem.call
    original_handles_tool = SkillSystem.handles_tool
    original_worker = SkillBuilder._build_worker

    def patched_registry_reload(self: Any) -> None:
        original_registry_reload(self)
        disabled = read_disabled()
        if disabled:
            self.skills = {
                skill_id: definition
                for skill_id, definition in self.skills.items()
                if skill_id not in disabled
            }
            self.logger.info("SKILLS | locally disabled: %s", ", ".join(sorted(disabled)))

    def patched_system_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_system_init(self, *args, **kwargs)
        self.shared_manager = SharedSkillManager(self, self.logger)
        self.builder.shared_manager = self.shared_manager

    def patched_system_shutdown(self: Any) -> None:
        manager = getattr(self, "shared_manager", None)
        if manager is not None:
            manager.stop()
        original_system_shutdown(self)

    def patched_schemas(self: Any) -> list[dict[str, Any]]:
        schemas = original_schemas(self)
        schemas.extend(
            [
                {
                    "type": "function",
                    "name": "set_local_skill_enabled",
                    "description": "Enable or disable a Jarvis skill only on this computer.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string"},
                            "enabled": {"type": "boolean"},
                        },
                        "required": ["skill_id", "enabled"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "sync_shared_skills",
                    "description": "Download shared Jarvis skills from GitHub now.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "submit_jarvis_feature_request",
                    "description": (
                        "Post a GitHub suggestion when a capability cannot be built "
                        "safely as a generated skill."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "suggested_name": {"type": "string"},
                            "reason": {"type": "string"},
                            "design": {"type": "string"},
                        },
                        "required": ["goal", "suggested_name", "reason", "design"],
                        "additionalProperties": False,
                    },
                },
            ]
        )
        return schemas

    def patched_handles_tool(self: Any, name: str) -> bool:
        return name in {
            "set_local_skill_enabled",
            "sync_shared_skills",
            "submit_jarvis_feature_request",
        } or original_handles_tool(self, name)

    def patched_call(self: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        manager = getattr(self, "shared_manager", None)
        if name == "set_local_skill_enabled":
            if manager is None:
                raise RuntimeError("Shared skill manager is unavailable.")
            return manager.set_enabled(str(args["skill_id"]), bool(args["enabled"]))
        if name == "sync_shared_skills":
            if manager is None:
                raise RuntimeError("Shared skill manager is unavailable.")
            return manager.sync()
        if name == "submit_jarvis_feature_request":
            if manager is None:
                raise RuntimeError("Shared skill manager is unavailable.")
            return manager.post_feature_request(
                goal=str(args["goal"]),
                suggested_name=str(args["suggested_name"]),
                reason=str(args["reason"]),
                design=str(args["design"]),
            )
        if name == "list_installed_skills" and manager is not None:
            return {"skills": manager.list_state()}
        return original_call(self, name, args)

    def patched_worker(
        self: Any,
        reporter: Any,
        goal: str,
        name: str,
        design: str,
    ) -> dict[str, Any]:
        manager = getattr(self, "shared_manager", None)
        try:
            result = original_worker(self, reporter, goal, name, design)
        except Exception as exc:
            if manager is not None:
                try:
                    issue = manager.post_feature_request(
                        goal=goal,
                        suggested_name=name,
                        reason=f"Skill generation or testing failed: {exc}",
                        design=design,
                    )
                    if issue.get("posted"):
                        raise RuntimeError(f"{exc} Feature request: {issue['url']}") from exc
                except RuntimeError:
                    raise
                except Exception:
                    self.logger.exception("SHARED SKILLS | issue creation failed")
            raise

        if manager is None:
            return result
        from .language_mode import detect_language
        language = detect_language(goal, "en")
        if result.get("installed") and result.get("skill_id"):
            reporter.update("Sharing", "Publishing the tested skill to GitHub.", 0.97)
            try:
                published = manager.publish_skill(str(result["skill_id"]))
            except Exception as exc:
                self.logger.exception("SHARED SKILLS | publication failed")
                published = {"published": False, "error": str(exc)}
            result["shared_publish"] = published
            if published.get("published"):
                result["spoken_summary"] = (
                    str(result.get("spoken_summary", "Skill ready."))
                    + (
                        " Den delas nu med dina andra Jarvis-installationer."
                        if language == "sv"
                        else " It is now shared with your other Jarvis instances."
                    )
                )
        elif not result.get("installed"):
            reason = str(result.get("summary") or "The generated skill was not buildable.")
            try:
                issue = manager.post_feature_request(
                    goal=goal,
                    suggested_name=name,
                    reason=reason,
                    design=design,
                )
            except Exception as exc:
                issue = {"posted": False, "error": str(exc)}
            result["feature_request"] = issue
            if issue.get("posted"):
                result["spoken_summary"] = (
                    str(result.get("spoken_summary", reason))
                    + (
                        " Jag skickade förslaget till GitHub för utvecklargranskning."
                        if language == "sv"
                        else " I posted it to GitHub for developer review."
                    )
                )
        return result

    SkillRegistry.reload = patched_registry_reload
    SkillSystem.__init__ = patched_system_init
    SkillSystem.shutdown = patched_system_shutdown
    SkillSystem.schemas = patched_schemas
    SkillSystem.handles_tool = patched_handles_tool
    SkillSystem.call = patched_call
    SkillBuilder._build_worker = patched_worker
    _PATCHED = True
