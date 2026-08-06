from __future__ import annotations

from typing import Any


_PATCHED = False


def _runtime_parameter_schema(
    parameters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow optional skill arguments to be omitted or explicitly null."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in parameters:
        name = str(parameter["name"])
        base_type = str(parameter["type"])
        is_required = bool(parameter.get("required"))
        properties[name] = {
            "type": base_type if is_required else [base_type, "null"],
            "description": str(parameter.get("description", "")),
        }
        if is_required:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .skill_schema import SkillRegistry

    SkillRegistry.parameter_schema = staticmethod(_runtime_parameter_schema)
    _PATCHED = True
