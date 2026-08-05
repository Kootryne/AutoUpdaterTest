from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import uuid4

from .paths import DATA_DIR
from .settings import Settings
from .skill_builder import SkillBuilder
from .skill_runtime import SkillRuntime
from .skill_schema import SkillRegistry
from .tasks import TaskManager


class SkillSystem:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.pending_path = DATA_DIR / "pending_skill.json"
        self.tasks = TaskManager(
            logger, max_workers=settings.background_task_workers
        )
        self.registry = SkillRegistry(logger)
        self.runtime = SkillRuntime(settings, logger, self.tasks)
        self.builder = SkillBuilder(
            settings, logger, self.tasks, self.registry, self.runtime
        )

    def schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": "suggest_new_skill",
                "description": (
                    "Use this when Jarvis cannot currently perform a requested "
                    "capability. Save a concrete proposal, then explain briefly "
                    "that Jarvis cannot do it yet and how the skill would work."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "requested_capability": {"type": "string"},
                        "suggested_name": {"type": "string"},
                        "how_it_would_work": {"type": "string"},
                        "likely_background": {"type": "boolean"},
                    },
                    "required": [
                        "requested_capability", "suggested_name",
                        "how_it_would_work", "likely_background"
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "build_new_skill",
                "description": (
                    "Start building a skill only after the user explicitly asks "
                    "Jarvis to make, build, create, or program it."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "requested_capability": {"type": ["string", "null"]},
                        "suggested_name": {"type": ["string", "null"]},
                        "how_it_would_work": {"type": ["string", "null"]},
                    },
                    "required": [
                        "requested_capability", "suggested_name", "how_it_would_work"
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "get_background_status",
                "description": (
                    "Get progress or the result of a background skill build or "
                    "long-running skill. Omit task_id to use the latest task."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": ["string", "null"]},
                        "include_result": {"type": "boolean"},
                    },
                    "required": ["task_id", "include_result"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "list_installed_skills",
                "description": "List the user-created skills installed in Jarvis.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]
        schemas.extend(self.registry.tool_schemas())
        return schemas

    def handles_tool(self, name: str) -> bool:
        return name in {
            "suggest_new_skill", "build_new_skill", "get_background_status",
            "list_installed_skills"
        } or self.registry.by_tool_name(name) is not None

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "suggest_new_skill":
            return self.suggest(
                requested_capability=str(args["requested_capability"]),
                suggested_name=str(args["suggested_name"]),
                how_it_would_work=str(args["how_it_would_work"]),
                likely_background=bool(args["likely_background"]),
            )
        if name == "build_new_skill":
            return self.start_build(
                requested_capability=args.get("requested_capability"),
                suggested_name=args.get("suggested_name"),
                how_it_would_work=args.get("how_it_would_work"),
            )
        if name == "get_background_status":
            return self.tasks.status_payload(
                args.get("task_id"),
                include_result=bool(args.get("include_result", True)),
            )
        if name == "list_installed_skills":
            return {
                "skills": [
                    {
                        "id": skill.id,
                        "name": skill.name,
                        "description": skill.manifest["description"],
                        "version": skill.manifest["version"],
                    }
                    for skill in self.registry.skills.values()
                ]
            }
        definition = self.registry.by_tool_name(name)
        if definition is not None:
            return self.runtime.invoke(definition, args)
        raise ValueError(f"Unknown skill tool: {name}")

    def suggest(
        self,
        *,
        requested_capability: str,
        suggested_name: str,
        how_it_would_work: str,
        likely_background: bool,
    ) -> dict[str, Any]:
        proposal = {
            "id": uuid4().hex[:10],
            "requested_capability": requested_capability.strip(),
            "suggested_name": suggested_name.strip() or "New skill",
            "how_it_would_work": how_it_would_work.strip(),
            "likely_background": likely_background,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_path = self.pending_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temp_path.replace(self.pending_path)
        self.logger.info(
            "SKILLS | saved proposal %s | %s",
            proposal["id"], proposal["suggested_name"]
        )
        return {"saved": True, "proposal": proposal}

    def pending(self) -> dict[str, Any] | None:
        try:
            proposal = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return proposal if isinstance(proposal, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)

    def start_pending_build(self) -> dict[str, Any]:
        proposal = self.pending()
        if proposal is None:
            return {"started": False, "error": "There is no pending skill proposal."}
        return self.start_build(
            requested_capability=proposal.get("requested_capability"),
            suggested_name=proposal.get("suggested_name"),
            how_it_would_work=proposal.get("how_it_would_work"),
        )

    def start_build(
        self,
        *,
        requested_capability: Any,
        suggested_name: Any,
        how_it_would_work: Any,
    ) -> dict[str, Any]:
        proposal = self.pending() or {}
        goal = str(
            requested_capability or proposal.get("requested_capability") or ""
        ).strip()
        name = str(
            suggested_name or proposal.get("suggested_name") or "New skill"
        ).strip()
        design = str(
            how_it_would_work or proposal.get("how_it_would_work") or ""
        ).strip()
        if not goal:
            return {"started": False, "error": "No skill goal was provided."}
        result = self.builder.start(goal, name, design)
        self.clear_pending()
        return result

    def prompt_context(self) -> str:
        pending = self.pending()
        pending_text = (
            json.dumps(pending, ensure_ascii=False)
            if pending else "No pending skill proposal."
        )
        return (
            "INSTALLED USER SKILLS:\n"
            f"{self.registry.summary()}\n\n"
            "PENDING SKILL PROPOSAL:\n"
            f"{pending_text}\n\n"
            "BACKGROUND TASKS:\n"
            f"{self.tasks.prompt_context()}"
        )

    def spoken_status(self, *, swedish: bool = False) -> str:
        return self.tasks.spoken_status(swedish=swedish)

    def has_active_tasks(self) -> bool:
        return self.tasks.has_active()

    def shutdown(self) -> None:
        self.tasks.shutdown()
