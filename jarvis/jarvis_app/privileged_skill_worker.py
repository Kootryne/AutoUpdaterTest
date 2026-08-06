from __future__ import annotations

import ast
import datetime
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
from typing import Any


APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from jarvis_app.privileged_skill_api import ALL_PERMISSIONS, SkillAPI  # noqa: E402


BANNED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "help",
    "breakpoint", "globals", "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "memoryview", "super", "type", "object", "classmethod",
    "staticmethod", "property", "exit", "quit",
}

SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "bytes": bytes,
    "bytearray": bytearray, "dict": dict, "enumerate": enumerate,
    "filter": filter, "float": float, "int": int, "isinstance": isinstance,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "next": next, "pow": pow, "print": print, "range": range,
    "reversed": reversed, "round": round, "set": set, "slice": slice,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "RuntimeError": RuntimeError,
    "ZeroDivisionError": ZeroDivisionError,
}


class CapabilityValidator(ast.NodeVisitor):
    def visit_Import(self, node: ast.Import) -> None:
        raise ValueError("Imports are not allowed. Use the permission-checked api object.")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise ValueError("Imports are not allowed. Use the permission-checked api object.")

    def visit_Global(self, node: ast.Global) -> None:
        raise ValueError("Global statements are not allowed.")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        raise ValueError("Nonlocal statements are not allowed.")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or "__" in node.attr:
            raise ValueError(f"Private attribute access is not allowed: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES or "__" in node.id:
            raise ValueError(f"Unsafe name is not allowed: {node.id}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in BANNED_NAMES:
            raise ValueError(f"Unsafe call is not allowed: {node.func.id}")
        self.generic_visit(node)


def validate_source(source: str) -> ast.Module:
    if len(source) > 160_000:
        raise ValueError("Generated skill code is too large.")
    tree = ast.parse(source, filename="skill.py", mode="exec")
    CapabilityValidator().visit(tree)
    runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
    if not runs:
        raise ValueError("Generated Python skill must define run(payload, api).")
    run = runs[0]
    positional = len(run.args.posonlyargs) + len(run.args.args)
    if positional < 2 and run.args.vararg is None:
        raise ValueError("Privileged Python skills must define run(payload, api).")
    return tree


def load_manifest(skill_dir: Path) -> dict[str, Any]:
    value = json.loads((skill_dir / "skill.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Skill manifest is not an object.")
    return value


def approved_permissions(manifest: dict[str, Any]) -> set[str]:
    requested = {str(item) for item in manifest.get("permissions", [])}
    unknown = requested - set(ALL_PERMISSIONS)
    if unknown:
        raise ValueError(f"Unknown skill permissions: {sorted(unknown)}")
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        raise PermissionError("This skill has not been approved by the user.")
    approved = {str(item) for item in approval.get("permissions", [])}
    if approved != requested:
        raise PermissionError("Approved permissions do not match the skill manifest.")
    return approved


def execute(skill_dir: Path, payload: dict[str, Any], test_mode: bool) -> Any:
    manifest = load_manifest(skill_dir)
    permissions = approved_permissions(manifest)
    source_path = skill_dir / "skill.py"
    tree = validate_source(source_path.read_text(encoding="utf-8"))
    api = SkillAPI(
        skill_id=str(manifest["id"]),
        permissions=permissions,
        skill_dir=skill_dir,
        test_mode=test_mode,
    )
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "math": math,
        "statistics": statistics,
        "re": re,
        "json": json,
        "datetime": datetime,
    }
    exec(compile(tree, str(source_path), "exec"), namespace, namespace)
    result = namespace["run"](payload, api)
    json.dumps(result, ensure_ascii=False, default=str)
    return result


def main() -> int:
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "Invalid worker arguments."}))
        return 2
    try:
        skill_dir = Path(sys.argv[1]).resolve()
        payload = json.loads(sys.argv[2])
        if not isinstance(payload, dict):
            raise ValueError("Skill input must be a JSON object.")
        test_mode = os.getenv("JARVIS_SKILL_TEST_MODE", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        result = execute(skill_dir, payload, test_mode)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        ))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
