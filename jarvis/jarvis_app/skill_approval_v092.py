from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from .paths import DATA_DIR
from .privileged_skill_api import ALL_PERMISSIONS, permission_labels, permission_risks


_PATCHED = False
_PENDING = DATA_DIR / "pending_skill_approval_v092.json"


def _write(value: dict[str, Any]) -> None:
    _PENDING.parent.mkdir(parents=True, exist_ok=True)
    temporary = _PENDING.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(_PENDING)


def _read() -> dict[str, Any] | None:
    try:
        value = json.loads(_PENDING.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _clear() -> None:
    _PENDING.unlink(missing_ok=True)


def patch_skill_approval_flow() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .skills import SkillSystem

    original_schemas = SkillSystem.schemas
    original_call = SkillSystem.call
    original_prompt_context = SkillSystem.prompt_context
    original_instructions = Brain.instructions

    def prepare(
        self: SkillSystem,
        *,
        requested_capability: Any,
        suggested_name: Any,
        how_it_would_work: Any,
    ) -> dict[str, Any]:
        existing = _read()
        if existing:
            return {
                "started": False,
                "approval_required": True,
                "error": "Another skill is already awaiting approval.",
                "pending": {
                    key: existing.get(key)
                    for key in (
                        "proposal_id",
                        "skill_name",
                        "permissions",
                        "permission_labels",
                        "risks",
                    )
                },
            }

        old = self.pending() or {}
        goal = str(
            requested_capability or old.get("requested_capability") or ""
        ).strip()
        name = str(
            suggested_name or old.get("suggested_name") or "New skill"
        ).strip()
        design = str(
            how_it_would_work or old.get("how_it_would_work") or ""
        ).strip()
        if not goal:
            return {"started": False, "error": "No skill goal was provided."}

        plan = self.builder._planner_call(goal, name, design)
        if not bool(plan.get("buildable")):
            return {
                "started": False,
                "approval_required": False,
                "error": str(
                    plan.get("block_reason")
                    or "The requested skill could not be planned."
                ),
                "plan": plan,
            }

        permissions = [str(v) for v in plan.get("permissions", [])]
        if plan.get("kind") == "workflow" and "model" not in permissions:
            permissions.insert(0, "model")
        permissions = list(dict.fromkeys(permissions))
        unknown = set(permissions) - set(ALL_PERMISSIONS)
        if unknown:
            raise ValueError(
                f"Planner requested unknown permissions: {sorted(unknown)}"
            )
        plan["permissions"] = permissions
        pending = {
            "proposal_id": uuid4().hex[:12],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "goal": goal,
            "skill_name": plan.get("name") or name,
            "description": plan.get("description"),
            "plan": plan,
            "permissions": permissions,
            "permission_labels": permission_labels(permissions),
            "risks": permission_risks(permissions),
            "status": "awaiting_approval",
        }
        _write(pending)
        self.clear_pending()
        return {
            "started": False,
            "approval_required": True,
            "proposal_id": pending["proposal_id"],
            "skill_name": pending["skill_name"],
            "description": pending["description"],
            "kind": plan.get("kind"),
            "permissions": permissions,
            "permission_labels": pending["permission_labels"],
            "risks": pending["risks"],
            "instruction": (
                "Briefly explain the permissions and meaningful risks. Ask one "
                "explicit yes/no question. Do not start building until a later "
                "user turn explicitly approves."
            ),
        }

    def approve(self: SkillSystem) -> dict[str, Any]:
        pending = _read()
        if not pending:
            return {
                "started": False,
                "approved": False,
                "error": "There is no skill awaiting approval.",
            }
        plan = pending.get("plan")
        if not isinstance(plan, dict):
            _clear()
            return {
                "started": False,
                "approved": False,
                "error": "The pending skill plan is invalid.",
            }
        plan["_approval"] = {
            "proposal_id": pending["proposal_id"],
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "permissions": list(pending.get("permissions", [])),
            "risks": list(pending.get("risks", [])),
        }
        result = self.builder.start_approved(plan)
        _clear()
        return {
            **result,
            "approved": True,
            "skill_name": plan.get("name"),
        }

    def cancel(self: SkillSystem) -> dict[str, Any]:
        pending = _read()
        _clear()
        return {
            "cancelled": pending is not None,
            "proposal_id": pending.get("proposal_id") if pending else None,
        }

    def schemas(self: SkillSystem) -> list[dict[str, Any]]:
        schemas = [
            schema
            for schema in original_schemas(self)
            if schema.get("name") not in {"suggest_new_skill", "build_new_skill"}
        ]
        schemas.extend(
            [
                {
                    "type": "function",
                    "name": "build_new_skill",
                    "description": (
                        "Plan a requested skill and return its exact permissions "
                        "and risks. This never installs the skill yet."
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
                            "requested_capability",
                            "suggested_name",
                            "how_it_would_work",
                        ],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "approve_skill_build",
                    "description": (
                        "Approve and start the pending skill only after the user "
                        "explicitly says yes after hearing permissions and risks."
                    ),
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
                    "name": "cancel_skill_build",
                    "description": "Cancel the pending skill plan.",
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
                    "name": "get_pending_skill_approval",
                    "description": "Inspect the skill currently awaiting approval.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            ]
        )
        return schemas

    def call(
        self: SkillSystem,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "build_new_skill":
            return self.prepare_privileged_build(
                requested_capability=args.get("requested_capability"),
                suggested_name=args.get("suggested_name"),
                how_it_would_work=args.get("how_it_would_work"),
            )
        if name == "approve_skill_build":
            return self.approve_privileged_build()
        if name == "cancel_skill_build":
            return self.cancel_privileged_build()
        if name == "get_pending_skill_approval":
            pending = _read()
            return {
                "pending": bool(pending),
                "proposal": (
                    {
                        key: pending.get(key)
                        for key in (
                            "proposal_id",
                            "skill_name",
                            "permissions",
                            "permission_labels",
                            "risks",
                        )
                    }
                    if pending
                    else None
                ),
            }
        result = original_call(self, name, args)
        if name == "list_installed_skills":
            for item in result.get("skills", []):
                definition = self.registry.skills.get(str(item.get("id")))
                if not definition:
                    continue
                manifest = definition.manifest
                item["permissions"] = manifest.get("permissions", [])
                item["permission_labels"] = permission_labels(
                    manifest.get("permissions", [])
                )
                item["approved_at"] = manifest.get("approval", {}).get(
                    "approved_at"
                )
        return result

    def handles_tool(self: SkillSystem, name: str) -> bool:
        return name in {
            "build_new_skill",
            "approve_skill_build",
            "cancel_skill_build",
            "get_pending_skill_approval",
            "get_background_status",
            "list_installed_skills",
        } or self.registry.by_tool_name(name) is not None

    def prompt_context(self: SkillSystem) -> str:
        base = original_prompt_context(self)
        pending = _read()
        compact = (
            {
                key: pending.get(key)
                for key in (
                    "proposal_id",
                    "skill_name",
                    "permissions",
                    "permission_labels",
                    "risks",
                )
            }
            if pending
            else None
        )
        return (
            f"{base}\n\nPENDING SKILL PERMISSION APPROVAL:\n"
            f"{json.dumps(compact, ensure_ascii=False)}\n\n"
            "Installed approved skills do not request permission again at runtime."
        )

    def instructions(self: Brain) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "SELF-CREATED PRIVILEGED SKILLS\n"
            "- Jarvis can create skills with approved web requests, cameras, "
            "screenshots, file access, program execution, keyboard/mouse, clipboard, "
            "environment, and system-information permissions.\n"
            "- Do not create a GitHub core-feature request merely because a skill "
            "needs one of those permissions.\n"
            "- First call build_new_skill. Explain its returned permissions and "
            "meaningful risks, then ask one explicit yes/no question.\n"
            "- Only after a later explicit yes call approve_skill_build. On no, call "
            "cancel_skill_build.\n"
            "- The approval is stored with the skill. Do not ask again whenever the "
            "approved skill runs.\n"
            "- PC shutdown and restart remain separate double-confirmed actions.\n"
        )

    SkillSystem.prepare_privileged_build = prepare
    SkillSystem.approve_privileged_build = approve
    SkillSystem.cancel_privileged_build = cancel
    SkillSystem.start_build = prepare
    SkillSystem.schemas = schemas
    SkillSystem.call = call
    SkillSystem.handles_tool = handles_tool
    SkillSystem.prompt_context = prompt_context
    Brain.instructions = instructions
    _PATCHED = True
