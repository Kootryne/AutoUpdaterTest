from __future__ import annotations

from functools import wraps
import os
import re
from typing import Any

from .paths import ENV_FILE


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


def _read_env_file() -> dict[str, str]:
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _write_env_values(changes: dict[str, str]) -> None:
    if not changes:
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    pending = dict(changes)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)

    if pending and output and output[-1].strip():
        output.append("")
    for key, value in pending.items():
        output.append(f"{key}={value}")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENV_FILE.with_suffix(".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(ENV_FILE)


def _migrate_model_policy() -> None:
    file_values = _read_env_file()

    def current(name: str) -> str:
        return os.getenv(name, file_values.get(name, "")).strip()

    changes: dict[str, str] = {}
    planner_alias = current("SKILL_PLANNER_ALIAS")
    planner_model = current("SKILL_PLANNER_MODEL")
    planner_reasoning = current("SKILL_PLANNER_REASONING")
    legacy_sol = (
        planner_alias.lower() == "sol"
        or "sol" in planner_model.lower().replace("_", "-")
    )
    if not planner_model or legacy_sol:
        changes["SKILL_PLANNER_MODEL"] = "gpt-5.6-terra"
        planner_model = changes["SKILL_PLANNER_MODEL"]
    if not planner_alias or legacy_sol or "terra" in planner_model.lower():
        changes["SKILL_PLANNER_ALIAS"] = "Terra"
    if not planner_reasoning or legacy_sol or "terra" in planner_model.lower():
        changes["SKILL_PLANNER_REASONING"] = "medium"

    builder_model = current("SKILL_BUILDER_MODEL") or "gpt-5.6-luna"
    runtime_model = current("SKILL_RUNTIME_MODEL") or "gpt-5.6-luna"
    if not current("SKILL_BUILDER_MODEL"):
        changes["SKILL_BUILDER_MODEL"] = builder_model
    if not current("SKILL_RUNTIME_MODEL"):
        changes["SKILL_RUNTIME_MODEL"] = runtime_model
    if "luna" in builder_model.lower():
        changes["SKILL_BUILDER_ALIAS"] = "Luna"
        changes["SKILL_BUILDER_REASONING"] = "none"
    if "luna" in runtime_model.lower():
        changes["SKILL_RUNTIME_REASONING"] = "none"

    _write_env_values(changes)
    for key, value in changes.items():
        os.environ[key] = value


def _install_openai_reasoning_policy() -> None:
    """Force Terra medium and Luna no-reasoning for every Responses API call."""
    from openai import OpenAI

    probe = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or "sk-local-policy-probe")
    responses_type = type(probe.responses)
    original_create = responses_type.create
    if getattr(original_create, "_jarvis_model_policy_v096", False):
        probe.close()
        return

    @wraps(original_create)
    def create_with_policy(self: Any, *args: Any, **kwargs: Any) -> Any:
        model = str(kwargs.get("model") or "")
        normalized = model.lower().replace("_", "-")
        if "luna" in normalized:
            kwargs["reasoning"] = {"effort": "none"}
        elif "terra" in normalized:
            kwargs["reasoning"] = {"effort": "medium", "summary": "concise"}
            instructions = kwargs.get("instructions")
            if isinstance(instructions, str):
                kwargs["instructions"] = re.sub(
                    r"\bSol\b",
                    "Terra",
                    instructions,
                    flags=re.IGNORECASE,
                )
        return original_create(self, *args, **kwargs)

    create_with_policy._jarvis_model_policy_v096 = True  # type: ignore[attr-defined]
    responses_type.create = create_with_policy
    probe.close()


def _install_parakeet_dependency_check() -> None:
    from .local_stt import ParakeetLocalSTT

    original_load = ParakeetLocalSTT._load_on
    if getattr(original_load, "_jarvis_librosa_check_v096", False):
        return

    @wraps(original_load)
    def load_with_dependency_check(
        self: Any,
        device: str,
        dtype: Any,
    ) -> tuple[Any, Any, Any]:
        try:
            import librosa  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "Local Parakeet STT requires librosa. Run the Jarvis updater "
                "again so the new audio dependencies are installed."
            ) from exc
        return original_load(self, device, dtype)

    load_with_dependency_check._jarvis_librosa_check_v096 = True  # type: ignore[attr-defined]
    ParakeetLocalSTT._load_on = load_with_dependency_check


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .skill_schema import SkillRegistry

    _migrate_model_policy()
    _install_openai_reasoning_policy()
    _install_parakeet_dependency_check()
    SkillRegistry.parameter_schema = staticmethod(_runtime_parameter_schema)
    _PATCHED = True
