from __future__ import annotations

import ast
import datetime
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any


BANNED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "help",
    "breakpoint", "globals", "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "memoryview", "super", "type", "object", "classmethod",
    "staticmethod", "property", "exit", "quit",
}

SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "pow": pow,
    "print": print,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "ZeroDivisionError": ZeroDivisionError,
}


class SafetyValidator(ast.NodeVisitor):
    def visit_Import(self, node: ast.Import) -> None:
        raise ValueError("Imports are not allowed in generated Python skills.")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise ValueError("Imports are not allowed in generated Python skills.")

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
    if len(source) > 80_000:
        raise ValueError("Generated skill code is too large.")
    tree = ast.parse(source, filename="skill.py", mode="exec")
    SafetyValidator().visit(tree)
    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    if "run" not in functions:
        raise ValueError("Generated Python skill must define run(payload).")
    return tree


def execute(skill_dir: Path, payload: dict[str, Any]) -> Any:
    source_path = skill_dir / "skill.py"
    source = source_path.read_text(encoding="utf-8")
    tree = validate_source(source)
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "math": math,
        "statistics": statistics,
        "re": re,
        "json": json,
        "datetime": datetime,
    }
    exec(compile(tree, str(source_path), "exec"), namespace, namespace)
    result = namespace["run"](payload)
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
        result = execute(skill_dir, payload)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
