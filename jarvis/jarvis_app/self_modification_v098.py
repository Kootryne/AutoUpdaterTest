from __future__ import annotations

from datetime import datetime, timezone
import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4

from .paths import APP_DIR, DATA_DIR, LOG_DIR

_PATCHED = False
_PENDING = DATA_DIR / "pending_core_change_v098.json"
_PATCH = DATA_DIR / "self_patch.py"
_META = DATA_DIR / "self_patch.json"
_HISTORY = DATA_DIR / "self_patch_history"
_RESTART = DATA_DIR / "self_patch_restart.json"
_LOG = LOG_DIR / "self_modification.log"
_MAX_HISTORY = 8
_MAX_CONTEXT = 70000

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "risk_summary": {"type": "string"},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "affected_components": {"type": "array", "items": {"type": "string"}},
        "implementation_plan": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "buildable": {"type": "boolean"},
        "block_reason": {"type": ["string", "null"]},
    },
    "required": ["title", "summary", "risk_level", "risk_summary", "dependencies", "affected_components", "implementation_plan", "tests", "buildable", "block_reason"],
    "additionalProperties": False,
}

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "patch_code": {"type": "string"},
        "implementation_summary": {"type": "string"},
        "self_review": {"type": "string"},
    },
    "required": ["patch_code", "implementation_summary", "self_review"],
    "additionalProperties": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _pending() -> dict[str, Any] | None:
    value = _read_json(_PENDING)
    return value if isinstance(value, dict) else None


def _clear_pending() -> None:
    _PENDING.unlink(missing_ok=True)


def _source_context(request: str) -> dict[str, str]:
    tokens = {t for t in re.findall(r"[a-zA-Z0-9_]{4,}", request.lower()) if t not in {"jarvis", "make", "change", "that", "this", "with"}}
    preferred = {"app.py", "brain.py", "tools.py", "settings.py", "skills.py", "process_controls.py", "voice_settings_v092.py", "reliability_v090.py"}
    candidates: list[tuple[int, Path, str]] = []
    for path in (APP_DIR / "jarvis_app").glob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        haystack = (path.name + "\n" + text[:45000]).lower()
        score = 30 if path.name in preferred else 0
        for token in tokens:
            score += (20 if token in path.name.lower() else 0) + min(12, haystack.count(token))
        candidates.append((score, path, text))
    candidates.sort(key=lambda x: (x[0], x[1].name), reverse=True)
    result: dict[str, str] = {}
    used = 0
    try:
        root = (APP_DIR / "jarvis.py").read_text(encoding="utf-8")[:12000]
        result["jarvis.py"] = root
        used += len(root)
    except OSError:
        pass
    for _, path, text in candidates:
        snippet = text[:18000]
        if used + len(snippet) > _MAX_CONTEXT:
            continue
        result[f"jarvis_app/{path.name}"] = snippet
        used += len(snippet)
        if used >= _MAX_CONTEXT - 5000:
            break
    return result


def _dependency(value: str) -> str:
    value = str(value).strip()
    if not value or any(x in value for x in ("@", "://", ";", "--", "\n", "\r")):
        raise ValueError(f"Unsupported dependency: {value!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~].+)?", value):
        raise ValueError(f"Invalid dependency: {value}")
    return value


def _install_dependencies(items: list[str]) -> None:
    dependencies = list(dict.fromkeys(_dependency(v) for v in items))
    if not dependencies:
        return
    _log("pip install: " + ", ".join(dependencies))
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade-strategy", "only-if-needed", *dependencies],
        cwd=str(APP_DIR), capture_output=True, text=True, timeout=900, check=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0),
    )
    for output in (result.stdout, result.stderr):
        if output and output.strip():
            for line in output.strip().splitlines():
                _log("pip | " + line)
    if result.returncode:
        raise RuntimeError(f"Dependency installation failed with exit code {result.returncode}.")


def _validate_source(source: str) -> None:
    tree = ast.parse(source, filename="self_patch.py", mode="exec")
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
    for node in tree.body:
        if not isinstance(node, allowed):
            raise ValueError(f"Executable module-level statement is not allowed: {type(node).__name__}")
        if isinstance(node, ast.Expr) and not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise ValueError("Only a docstring may be an expression at module level.")
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "apply_patch" not in names or "self_test" not in names:
        raise ValueError("Generated core override must define apply_patch() and self_test().")


def _test_patch(path: Path) -> dict[str, Any]:
    runner = """
import importlib.util, json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ['JARVIS_CORE_MOD_TEST']='1'
spec=importlib.util.spec_from_file_location('jarvis_self_patch_test', sys.argv[2])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.apply_patch(); result=module.self_test()
if isinstance(result, bool): result={'passed':result}
if not isinstance(result, dict) or not bool(result.get('passed', False)):
    raise RuntimeError('self_test failed: '+repr(result))
print(json.dumps(result, ensure_ascii=False, default=str))
""".strip()
    result = subprocess.run(
        [sys.executable, "-I", "-c", runner, str(APP_DIR), str(path)], cwd=str(APP_DIR), capture_output=True, text=True, timeout=90, check=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0),
    )
    if result.returncode:
        raise RuntimeError("Isolated core test failed: " + (result.stderr.strip() or result.stdout.strip() or "unknown error"))
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Isolated core test returned no result.")
    payload = json.loads(lines[-1])
    if not isinstance(payload, dict) or not payload.get("passed"):
        raise RuntimeError(f"Core test did not pass: {payload!r}")
    return payload


def _backup_current() -> str | None:
    if not _PATCH.exists():
        return None
    _HISTORY.mkdir(parents=True, exist_ok=True)
    revision = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    directory = _HISTORY / revision
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copy2(_PATCH, directory / "self_patch.py")
    if _META.exists():
        shutil.copy2(_META, directory / "self_patch.json")
    revisions = sorted([p for p in _HISTORY.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    for stale in revisions[_MAX_HISTORY:]:
        shutil.rmtree(stale, ignore_errors=True)
    return revision


def _install_patch(pending: dict[str, Any], build: dict[str, Any]) -> dict[str, Any]:
    plan = pending["plan"]
    source = str(build["patch_code"])
    _validate_source(source)
    staging = Path(tempfile.mkdtemp(prefix="jarvis_core_", dir=DATA_DIR))
    try:
        candidate = staging / "self_patch.py"
        candidate.write_text(source, encoding="utf-8")
        test_result = _test_patch(candidate)
        revision = _backup_current()
        version = int((_read_json(_META, {}) or {}).get("version", 0)) + 1
        meta = {
            "version": version,
            "enabled": True,
            "updated_at": _now(),
            "request": pending["request"],
            "title": plan["title"],
            "summary": plan["summary"],
            "risk_level": plan["risk_level"],
            "risk_summary": plan["risk_summary"],
            "dependencies": plan["dependencies"],
            "affected_components": plan["affected_components"],
            "implementation_plan": plan["implementation_plan"],
            "tests": plan["tests"],
            "implementation_summary": build["implementation_summary"],
            "self_review": build["self_review"],
            "test_result": test_result,
            "previous_revision": revision,
            "last_error": None,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        candidate.replace(_PATCH)
        _write_json(_META, meta)
        _write_json(_RESTART, {"reason": "core_change", "created_at": _now()})
        return {"installed": True, "version": version, "summary": plan["summary"], "test_result": test_result, "restart_required": True}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _build(system: Any, pending: dict[str, Any]) -> dict[str, Any]:
    plan = pending["plan"]
    _install_dependencies([str(v) for v in plan.get("dependencies", [])])
    current_patch = _PATCH.read_text(encoding="utf-8") if _PATCH.exists() else ""
    instructions = """
You are Luna, Jarvis's core modification engineer. Reasoning is disabled. Produce one complete cumulative persistent override module that implements Terra's approved change while preserving all earlier behavior in CURRENT OVERRIDE unless the user asked to remove or replace it.

The module is loaded after all official Jarvis patches on every startup. It must define apply_patch() and self_test(). apply_patch() may monkeypatch Jarvis classes/functions, register tools, alter routing/prompts, add hooks, or change runtime behavior. It must only REGISTER behavior when loaded, not perform the user-facing action immediately. self_test() must verify the installed hooks and return {'passed': True, 'checks': [...]}.

No executable module-level statements except imports, constants, definitions, and a docstring. Do not edit installed Jarvis source files. Persist runtime state under jarvis_app.paths.DATA_DIR. Respect JARVIS_CORE_MOD_TEST=1 and avoid external side effects while testing. Preserve unrelated behavior and use existing Jarvis APIs where practical.
""".strip()
    errors: list[str] = []
    feedback = ""
    for attempt in range(system.settings.skill_build_retries + 1):
        build = system.builder._model_response(
            role=system.settings.skill_builder_alias,
            model=system.settings.skill_builder_model,
            reasoning=system.settings.skill_builder_reasoning,
            instructions=instructions,
            input_text=(
                "APPROVED TERRA PLAN:\n" + json.dumps(plan, ensure_ascii=False, indent=2)
                + "\n\nUSER REQUEST:\n" + pending["request"]
                + "\n\nCURRENT CUMULATIVE OVERRIDE:\n" + (current_patch or "None yet.")
                + "\n\nRELEVANT CURRENT JARVIS SOURCE:\n" + json.dumps(pending["source_context"], ensure_ascii=False)
                + "\n\nREPAIR FEEDBACK:\n" + (feedback or "None.")
            ),
            schema_name="jarvis_core_override_build",
            schema=BUILD_SCHEMA,
            max_output_tokens=16000,
        )
        try:
            result = _install_patch(pending, build)
            result["attempts"] = attempt + 1
            _clear_pending()
            return result
        except Exception as exc:
            errors.append(f"Attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            _log(errors[-1])
            if attempt >= system.settings.skill_build_retries:
                raise RuntimeError(" | ".join(errors)) from exc
            feedback = "Repair all failures without dropping previous override behavior:\n" + "\n".join(errors)
    raise RuntimeError("Core modification build ended unexpectedly.")


def _revisions() -> list[dict[str, Any]]:
    if not _HISTORY.exists():
        return []
    result = []
    for directory in sorted([p for p in _HISTORY.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True):
        meta = _read_json(directory / "self_patch.json", {})
        result.append({"revision": directory.name, "version": meta.get("version") if isinstance(meta, dict) else None, "title": meta.get("title") if isinstance(meta, dict) else None})
    return result


def _manage(action: str, revision: str | None) -> dict[str, Any]:
    if action == "status":
        return {"active": _PATCH.exists(), "metadata": _read_json(_META, {}), "pending": _pending() is not None}
    if action == "revisions":
        return {"revisions": _revisions()}
    if action == "cancel":
        existed = _pending() is not None
        _clear_pending()
        return {"cancelled": existed}
    if action == "disable":
        meta = _read_json(_META, {}) or {}
        meta["enabled"] = False; meta["updated_at"] = _now(); _write_json(_META, meta)
        _write_json(_RESTART, {"reason": "disable", "created_at": _now()})
        return {"changed": True, "enabled": False, "restart_required": True}
    if action == "enable":
        if not _PATCH.exists(): return {"changed": False, "error": "No self-modification exists."}
        meta = _read_json(_META, {}) or {}
        meta["enabled"] = True; meta["last_error"] = None; meta["updated_at"] = _now(); _write_json(_META, meta)
        _write_json(_RESTART, {"reason": "enable", "created_at": _now()})
        return {"changed": True, "enabled": True, "restart_required": True}
    if action == "clear":
        _backup_current(); _PATCH.unlink(missing_ok=True); _META.unlink(missing_ok=True)
        _write_json(_RESTART, {"reason": "clear", "created_at": _now()})
        return {"cleared": True, "restart_required": True}
    if action == "rollback":
        revisions = _revisions()
        selected = next((r for r in revisions if r["revision"] == revision), None) if revision else (revisions[0] if revisions else None)
        if selected is None: return {"restored": False, "error": "No matching revision exists."}
        source = _HISTORY / selected["revision"]
        _backup_current(); shutil.copy2(source / "self_patch.py", _PATCH)
        if (source / "self_patch.json").exists(): shutil.copy2(source / "self_patch.json", _META)
        _write_json(_RESTART, {"reason": "rollback", "created_at": _now()})
        return {"restored": True, "revision": selected["revision"], "restart_required": True}
    raise ValueError(f"Unknown self-modification action: {action}")


def _load_override(logger: logging.Logger) -> None:
    meta = _read_json(_META, {})
    if not _PATCH.exists() or (isinstance(meta, dict) and not bool(meta.get("enabled", True))):
        return
    try:
        spec = importlib.util.spec_from_file_location("jarvis_persistent_self_patch", _PATCH)
        if spec is None or spec.loader is None: raise RuntimeError("Could not load persistent override.")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        apply = getattr(module, "apply_patch", None)
        if not callable(apply): raise RuntimeError("Persistent override is missing apply_patch().")
        apply(); logger.info("CORE SELF-MOD | loaded version %s", meta.get("version", "?"))
    except Exception as exc:
        if not isinstance(meta, dict): meta = {}
        meta["enabled"] = False; meta["last_error"] = f"{type(exc).__name__}: {exc}"; meta["updated_at"] = _now(); _write_json(_META, meta)
        logger.exception("CORE SELF-MOD | disabled failing override")


def _restart_after_speech(jarvis: Any) -> None:
    if not _RESTART.exists(): return
    _RESTART.unlink(missing_ok=True)
    script = APP_DIR / "restart_jarvis.bat"
    if sys.platform == "win32" and script.exists():
        escaped = str(script).replace("'", "''")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", f"Start-Sleep -Milliseconds 900; & '{escaped}'"],
            cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        jarvis.exit_requested = True


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED: return
    from .app import Jarvis
    from .brain import Brain
    from .skills import SkillSystem
    from . import voice_settings_v092

    old_schemas = SkillSystem.schemas
    old_call = SkillSystem.call
    old_handles = SkillSystem.handles_tool
    old_context = SkillSystem.prompt_context
    old_instructions = Brain.instructions

    voice_settings_v092.EXTRA_DEFAULTS["CORE_SELF_MOD_ENABLED"] = True
    voice_settings_v092.ALIASES.update({"self modification": "CORE_SELF_MOD_ENABLED", "core self modification": "CORE_SELF_MOD_ENABLED"})

    def plan_core_change(self: SkillSystem, requested_change: str, context: str) -> dict[str, Any]:
        if os.getenv("CORE_SELF_MOD_ENABLED", "true").strip().lower() in {"0", "false", "no", "off", "disabled"}:
            return {"planned": False, "error": "Jarvis core self-modification is disabled."}
        request = requested_change.strip()
        if not request: return {"planned": False, "error": "No core change was provided."}
        if _pending(): return {"planned": False, "approval_required": True, "error": "Another core change is awaiting approval."}
        source_context = _source_context(request + " " + context)
        current_meta = _read_json(_META, {})
        instructions = """
You are Terra, Jarvis's medium-reasoning core-change architect. The user explicitly asked Jarvis to change how Jarvis itself works. Design a persistent local Python override, not a generated skill and not a GitHub issue.

The override loads after every official Jarvis release patch. It can monkeypatch classes/functions, register or alter tools, change routing/prompts/settings behavior, and add runtime hooks. It may request ordinary PyPI packages when genuinely needed. Preserve unrelated behavior and all previous cumulative self-modifications unless the request intentionally changes them. Mark buildable=false only when a persistent Python override cannot realistically implement the request.
""".strip()
        plan = self.builder._model_response(
            role=self.settings.skill_planner_alias, model=self.settings.skill_planner_model, reasoning=self.settings.skill_planner_reasoning,
            instructions=instructions,
            input_text="USER REQUEST:\n" + request + "\n\nCONTEXT:\n" + (context or "None.") + "\n\nCURRENT SELF-MOD METADATA:\n" + json.dumps(current_meta, ensure_ascii=False) + "\n\nRELEVANT SOURCE:\n" + json.dumps(source_context, ensure_ascii=False),
            schema_name="jarvis_core_override_plan", schema=PLAN_SCHEMA, max_output_tokens=9000,
        )
        if not plan.get("buildable"):
            return {"planned": False, "approval_required": False, "error": plan.get("block_reason") or "Not buildable as a persistent override.", "plan": plan}
        plan["dependencies"] = list(dict.fromkeys(_dependency(v) for v in plan.get("dependencies", [])))
        pending = {"proposal_id": uuid4().hex[:12], "created_at": _now(), "request": request, "context": context, "plan": plan, "source_context": source_context}
        _write_json(_PENDING, pending)
        return {"planned": True, "approval_required": True, "proposal_id": pending["proposal_id"], "title": plan["title"], "summary": plan["summary"], "risk_level": plan["risk_level"], "risk_summary": plan["risk_summary"], "dependencies": plan["dependencies"], "affected_components": plan["affected_components"], "implementation_plan": plan["implementation_plan"], "tests": plan["tests"], "instruction": "Explain the change and meaningful risk briefly, then ask one explicit yes/no question. Build only after a later yes."}

    def schemas(self: SkillSystem) -> list[dict[str, Any]]:
        result = [s for s in old_schemas(self) if s.get("name") not in {"plan_core_change", "approve_core_change", "manage_core_change"}]
        result += [
            {"type": "function", "name": "plan_core_change", "description": "Plan a persistent change to Jarvis itself after the user explicitly asks to add, fix, remove, or change Jarvis core behavior.", "strict": True, "parameters": {"type": "object", "properties": {"requested_change": {"type": "string"}, "context": {"type": ["string", "null"]}}, "required": ["requested_change", "context"], "additionalProperties": False}},
            {"type": "function", "name": "approve_core_change", "description": "Build, test, install, and restart for the pending Jarvis core change only after a later explicit user approval.", "strict": True, "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"type": "function", "name": "manage_core_change", "description": "Inspect, cancel, enable, disable, clear, list revisions, or rollback Jarvis's persistent core override.", "strict": True, "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "cancel", "enable", "disable", "clear", "revisions", "rollback"]}, "revision": {"type": ["string", "null"]}}, "required": ["action", "revision"], "additionalProperties": False}},
        ]
        return result

    def call(self: SkillSystem, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "plan_core_change": return self.plan_core_change(str(args["requested_change"]), str(args.get("context") or ""))
        if name == "approve_core_change":
            pending = _pending()
            if pending is None: return {"installed": False, "error": "No core change is awaiting approval."}
            return {**_build(self, pending), "approved": True}
        if name == "manage_core_change": return _manage(str(args["action"]), str(args["revision"]) if args.get("revision") else None)
        return old_call(self, name, args)

    def handles(self: SkillSystem, name: str) -> bool:
        return name in {"plan_core_change", "approve_core_change", "manage_core_change"} or old_handles(self, name)

    def context(self: SkillSystem) -> str:
        pending = _pending(); plan = pending.get("plan") if isinstance(pending, dict) else None
        compact = {k: plan.get(k) for k in ("title", "summary", "risk_level", "risk_summary", "dependencies")} if isinstance(plan, dict) else None
        return old_context(self) + "\n\nPERSISTENT CORE SELF-MODIFICATION:\n" + json.dumps(_read_json(_META, {}), ensure_ascii=False) + "\nPENDING CORE CHANGE:\n" + json.dumps(compact, ensure_ascii=False)

    def instructions(self: Brain) -> str:
        return old_instructions(self) + """

SELF-MODIFYING JARVIS CORE
- Use normal settings tools for ordinary configurable values and edit_installed_skill for an installed skill. When the user explicitly asks for a structural change to Jarvis itself, call plan_core_change. Do not substitute a generated skill or GitHub issue.
- Explain the returned change, dependencies, and meaningful risk, then ask one yes/no confirmation.
- On a later explicit yes call approve_core_change. On no call manage_core_change with action=cancel.
- The persistent cumulative override survives official Jarvis updates and is revisioned for rollback.
- Use manage_core_change for status, disable, enable, clear, revisions, or rollback.
- Only use the GitHub core-feature fallback if plan_core_change reports the request is genuinely not buildable.
""".rstrip()

    SkillSystem.plan_core_change = plan_core_change
    SkillSystem.schemas = schemas
    SkillSystem.call = call
    SkillSystem.handles_tool = handles
    SkillSystem.prompt_context = context
    Brain.instructions = instructions

    _load_override(logging.getLogger("jarvis"))
    current_local_skill = Jarvis._handle_local_skill_command
    current_say = Jarvis.say

    def local_skill(self: Jarvis, command: str, turn_started: float | None) -> bool:
        if _pending() is not None:
            return False
        return current_local_skill(self, command, turn_started)

    def say(self: Jarvis, text: str, *args: Any, **kwargs: Any) -> Any:
        result = current_say(self, text, *args, **kwargs)
        try:
            _restart_after_speech(self)
        except Exception:
            self.logger.exception("CORE SELF-MOD | restart scheduling failed")
        return result

    Jarvis._handle_local_skill_command = local_skill
    Jarvis.say = say
    _PATCHED = True
