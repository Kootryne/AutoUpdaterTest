from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Any

from .paths import SKILLS_DIR


SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
SAFE_PERMISSIONS = {"model", "web_search"}
PARAMETER_TYPES = {"string", "integer", "number", "boolean"}

PARAMETER_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "type": {
            "type": "string",
            "enum": ["string", "integer", "number", "boolean"],
        },
        "description": {"type": "string"},
        "required": {"type": "boolean"},
    },
    "required": ["name", "type", "description", "required"],
}

WORKFLOW_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "instructions": {"type": "string"},
        "use_web": {"type": "boolean"},
        "max_output_tokens": {
            "type": "integer",
            "minimum": 64,
            "maximum": 8000,
        },
    },
    "required": ["name", "instructions", "use_web", "max_output_tokens"],
}

TEST_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "input_json": {"type": "string"},
        "expected_contains": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["name", "input_json", "expected_contains"],
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "buildable": {"type": "boolean"},
        "block_reason": {"type": "string"},
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "kind": {"type": "string", "enum": ["workflow", "python"]},
        "background": {"type": "boolean"},
        "parameters": {"type": "array", "items": PARAMETER_ITEM_SCHEMA},
        "permissions": {
            "type": "array",
            "items": {"type": "string", "enum": ["model", "web_search"]},
        },
        "workflow_steps": {"type": "array", "items": WORKFLOW_STEP_SCHEMA},
        "python_requirements": {"type": "string"},
        "tests": {"type": "array", "items": TEST_ITEM_SCHEMA},
        "implementation_notes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "buildable", "block_reason", "id", "name", "description", "kind",
        "background", "parameters", "permissions", "workflow_steps",
        "python_requirements", "tests", "implementation_notes"
    ],
}

BUILD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "implementation_summary": {"type": "string"},
        "workflow_steps": {"type": "array", "items": WORKFLOW_STEP_SCHEMA},
        "python_code": {"type": "string"},
        "tests": {"type": "array", "items": TEST_ITEM_SCHEMA},
        "self_review": {"type": "string"},
    },
    "required": [
        "implementation_summary", "workflow_steps", "python_code", "tests",
        "self_review"
    ],
}


@dataclass(slots=True)
class SkillDefinition:
    directory: Path
    manifest: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def name(self) -> str:
        return str(self.manifest["name"])

    @property
    def tool_name(self) -> str:
        return f"skill_{self.id}"


class SkillRegistry:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.skills: dict[str, SkillDefinition] = {}
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        self.reload()

    @staticmethod
    def parameter_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in parameters:
            name = str(parameter["name"])
            properties[name] = {
                "type": str(parameter["type"]),
                "description": str(parameter.get("description", "")),
            }
            if bool(parameter.get("required")):
                required.append(name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @classmethod
    def validate_manifest(
        cls,
        manifest: dict[str, Any],
        directory: Path | None = None,
    ) -> None:
        required = {
            "schema_version", "id", "name", "version", "description", "kind",
            "background", "parameters", "permissions", "tests", "build"
        }
        missing = required - set(manifest)
        if missing:
            raise ValueError(f"Skill manifest is missing: {sorted(missing)}")
        skill_id = str(manifest["id"])
        if not SKILL_ID_RE.fullmatch(skill_id):
            raise ValueError(f"Invalid skill id: {skill_id}")
        if manifest["kind"] not in {"workflow", "python"}:
            raise ValueError("Skill kind must be workflow or python.")
        permissions = set(manifest.get("permissions", []))
        if not permissions <= SAFE_PERMISSIONS:
            raise ValueError(
                f"Unsupported skill permissions: {sorted(permissions - SAFE_PERMISSIONS)}"
            )
        parameters = manifest.get("parameters", [])
        if not isinstance(parameters, list):
            raise ValueError("Skill parameters must be a list.")
        seen: set[str] = set()
        for parameter in parameters:
            name = str(parameter.get("name", ""))
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", name):
                raise ValueError(f"Invalid parameter name: {name}")
            if name in seen:
                raise ValueError(f"Duplicate parameter: {name}")
            seen.add(name)
            if parameter.get("type") not in PARAMETER_TYPES:
                raise ValueError(f"Invalid parameter type for {name}")
        cls.parameter_schema(parameters)

        if manifest["kind"] == "workflow":
            steps = manifest.get("workflow_steps", [])
            if not isinstance(steps, list) or not steps:
                raise ValueError("Workflow skills require at least one step.")
            for step in steps:
                if not str(step.get("instructions", "")).strip():
                    raise ValueError("Every workflow step needs instructions.")
                if step.get("use_web") and "web_search" not in permissions:
                    raise ValueError("A web step requires web_search permission.")
        elif directory is not None and not (directory / "skill.py").is_file():
            raise ValueError("Python skill is missing skill.py.")

    def reload(self) -> None:
        loaded: dict[str, SkillDefinition] = {}
        for manifest_path in sorted(SKILLS_DIR.glob("*/skill.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("Manifest is not an object.")
                self.validate_manifest(manifest, manifest_path.parent)
                definition = SkillDefinition(manifest_path.parent, manifest)
                loaded[definition.id] = definition
            except Exception as exc:
                self.logger.error(
                    "SKILLS | failed to load %s: %s", manifest_path, exc
                )
        self.skills = loaded
        self.logger.info("SKILLS | loaded %d skill(s)", len(self.skills))

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": definition.tool_name,
                "description": (
                    f"Use the installed Jarvis skill '{definition.name}': "
                    f"{definition.manifest['description']}"
                ),
                "strict": True,
                "parameters": self.parameter_schema(
                    list(definition.manifest.get("parameters", []))
                ),
            }
            for definition in self.skills.values()
        ]

    def by_tool_name(self, tool_name: str) -> SkillDefinition | None:
        if not tool_name.startswith("skill_"):
            return None
        return self.skills.get(tool_name[len("skill_") :])

    def summary(self) -> str:
        if not self.skills:
            return "No user-created skills are installed."
        return "\n".join(
            f"- {skill.name} ({skill.id}): {skill.manifest['description']}"
            for skill in self.skills.values()
        )


class SilentReporter:
    def update(self, stage: str, detail: str = "", progress: float | None = None) -> None:
        return

    def event(self, message: str) -> None:
        return
