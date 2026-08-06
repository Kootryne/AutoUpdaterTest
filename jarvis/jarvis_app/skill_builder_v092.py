from __future__ import annotations

from datetime import datetime, timezone
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .paths import SKILL_STAGING_DIR
from .privileged_skill_api import (
    ALL_PERMISSIONS,
    BASIC_PERMISSIONS,
    PERMISSION_SPECS,
    permission_labels,
)


_PATCHED = False


def _run_worker(skill_dir: Path, args: dict[str, Any], timeout: float) -> Any:
    worker = Path(__file__).with_name("privileged_skill_worker.py")
    try:
        test_mode = skill_dir.resolve().is_relative_to(SKILL_STAGING_DIR.resolve())
    except Exception:
        test_mode = str(SKILL_STAGING_DIR.resolve()).lower() in str(
            skill_dir.resolve()
        ).lower()
    command = [
        sys.executable,
        "-I",
        str(worker),
        str(skill_dir),
        json.dumps(args, ensure_ascii=False),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(8.0, min(float(timeout), 600.0)),
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        ),
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "JARVIS_SKILL_TEST_MODE": "1" if test_mode else "0",
        },
    )
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(
            process.stderr.strip() or "Privileged skill worker returned no output."
        )
    payload = json.loads(lines[-1])
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error", "Privileged skill failed.")))
    return payload.get("result")


def _build_approved(builder: Any, reporter: Any, plan: dict[str, Any]) -> dict[str, Any]:
    reporter.update(
        "Programming",
        f"{builder.settings.skill_builder_alias} is implementing the approved plan.",
        0.25,
    )
    feedback = ""
    errors: list[str] = []
    for attempt in range(builder.settings.skill_build_retries + 1):
        build = builder._builder_call(plan, feedback)
        staging: Path | None = None
        try:
            reporter.update(
                "Validating",
                "Checking permissions, schemas, code, and approval integrity.",
                0.50,
            )
            staging, manifest = builder._write_staging(plan, build)
            reporter.update(
                "Testing",
                "Running tests; high-impact PC operations are simulated.",
                0.68,
            )
            report = builder.runtime.test_staged_skill(staging, manifest, reporter)
            if not report["passed"]:
                raise RuntimeError("; ".join(report["errors"]))
            reporter.update(
                "Installing",
                "Installing the approved skill atomically.",
                0.92,
            )
            destination = builder._install_staging(staging, manifest)
            staging = None
            builder.registry.reload()
            summary = f"{manifest['name']} is built, approved, tested, and ready."
            return {
                "installed": True,
                "skill_id": manifest["id"],
                "skill_name": manifest["name"],
                "path": str(destination),
                "permissions": manifest.get("permissions", []),
                "test_report": report,
                "summary": summary,
                "spoken_summary": summary,
            }
        except Exception as exc:
            errors.append(f"Attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            builder.logger.exception(
                "SKILLS | approved build attempt %d failed", attempt + 1
            )
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if attempt >= builder.settings.skill_build_retries:
                raise RuntimeError(" | ".join(errors)) from exc
            feedback = (
                "Repair every failure below without changing the approved "
                "permission set:\n" + "\n".join(errors)
            )
            reporter.update(
                "Repairing",
                f"{builder.settings.skill_builder_alias} is repairing the skill.",
                0.58,
            )
    raise RuntimeError("Approved skill build ended unexpectedly.")


def patch_builder_and_runtime() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .skill_builder import SkillBuilder
    from .skill_runtime import SkillRuntime
    from .skill_schema import (
        BUILD_SCHEMA,
        PLAN_SCHEMA,
        SAFE_PERMISSIONS,
        SkillRegistry,
    )

    SAFE_PERMISSIONS.update(ALL_PERMISSIONS)
    PLAN_SCHEMA["properties"]["permissions"]["items"]["enum"] = list(
        ALL_PERMISSIONS
    )

    original_validate = SkillRegistry.validate_manifest
    original_manifest = SkillBuilder._manifest_from_build
    original_validate_python = SkillBuilder._validate_python_source
    original_run_python = SkillRuntime.run_python

    def validate_manifest(
        cls: type[SkillRegistry],
        manifest: dict[str, Any],
        directory: Path | None = None,
    ) -> None:
        original_validate(manifest, directory)
        permissions = {str(v) for v in manifest.get("permissions", [])}
        unknown = permissions - set(ALL_PERMISSIONS)
        if unknown:
            raise ValueError(f"Unknown skill permissions: {sorted(unknown)}")
        if int(manifest.get("schema_version", 1)) >= 2:
            approval = manifest.get("approval")
            if not isinstance(approval, dict) or approval.get("status") != "approved":
                raise ValueError("Version 2 skills require user approval.")
            if {str(v) for v in approval.get("permissions", [])} != permissions:
                raise ValueError("Approved and requested permissions do not match.")

    def planner_call(
        self: SkillBuilder,
        goal: str,
        name: str,
        design: str,
    ) -> dict[str, Any]:
        permissions = "\n".join(
            f"- {key}: {spec['label']}. {spec['risk']}"
            for key, spec in PERMISSION_SPECS.items()
        )
        instructions = f"""
You are Sol, Jarvis's high-reasoning skill architect. Jarvis supports powerful
skills after one explicit user approval at creation. Do not reject a skill merely
because it needs web requests, camera, screenshots, files, programs, keyboard or
mouse, clipboard, environment variables, or system information. Request only the
permissions genuinely required and make the risk clear.

Skill kinds:
- workflow: model steps with optional OpenAI web search.
- python: run(payload, api), using the permission-checked SkillAPI.

Permissions:
{permissions}

SkillAPI:
api.model_text, api.web_search, api.http_request, api.capture_screenshot,
api.capture_camera, api.read_text, api.read_bytes_base64, api.list_files,
api.write_text, api.copy_path, api.delete_path, api.run_process, api.run_shell,
api.open_path, api.mouse_move, api.mouse_click, api.press_key, api.hotkey,
api.type_text, api.clipboard_get, api.clipboard_set, api.get_environment,
api.list_environment_names, and api.system_info.

Python skills may not import modules or bypass the api object. PC shutdown and
restart are never skill permissions; they remain double-confirmed Jarvis actions.
Produce realistic tests and a compact maintainable plan.
""".strip()
        return self._model_response(
            role=self.settings.skill_planner_alias,
            model=self.settings.skill_planner_model,
            reasoning=self.settings.skill_planner_reasoning,
            instructions=instructions,
            input_text=(
                f"Requested capability: {goal}\n"
                f"Suggested name: {name}\n"
                f"Initial design: {design or 'none'}"
            ),
            schema_name="jarvis_privileged_skill_plan",
            schema=PLAN_SCHEMA,
            max_output_tokens=7000,
        )

    def builder_call(
        self: SkillBuilder,
        plan: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        instructions = """
You are Luna, Jarvis's skill implementation engineer. Follow the approved plan
exactly and never add permissions. Workflow skills use precise model steps.
Python skills define run(payload, api), use no imports, and access the PC only
through the listed SkillAPI methods. Validate inputs, use bounded timeouts,
handle missing files/devices, and return JSON-serializable values. Automated
tests simulate writes, deletes, process execution, UI control, clipboard writes,
and opening paths.
""".strip()
        return self._model_response(
            role=self.settings.skill_builder_alias,
            model=self.settings.skill_builder_model,
            reasoning=self.settings.skill_builder_reasoning,
            instructions=instructions,
            input_text=(
                "APPROVED PLAN:\n"
                + json.dumps(plan, indent=2, ensure_ascii=False)
                + "\n\nREPAIR FEEDBACK:\n"
                + (feedback or "None.")
            ),
            schema_name="jarvis_privileged_skill_build",
            schema=BUILD_SCHEMA,
            max_output_tokens=10000,
        )

    def manifest_from_build(
        self: SkillBuilder,
        plan: dict[str, Any],
        build: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = original_manifest(self, plan, build)
        approval = plan.get("_approval")
        if not isinstance(approval, dict):
            raise ValueError("Skill build is missing user approval.")
        permissions = list(dict.fromkeys(manifest.get("permissions", [])))
        approved = [str(v) for v in approval.get("permissions", [])]
        if set(permissions) != set(approved):
            raise ValueError("Skill permissions changed after approval.")
        manifest["schema_version"] = 2
        manifest["approval"] = {
            "status": "approved",
            "proposal_id": str(approval["proposal_id"]),
            "approved_at": str(approval["approved_at"]),
            "permissions": approved,
            "permission_labels": permission_labels(approved),
            "risks_acknowledged": list(approval.get("risks", [])),
            "prompted_once": True,
        }
        manifest["runtime_timeout_seconds"] = 60
        return manifest

    def validate_python(source: str) -> None:
        original_validate_python(source)
        tree = ast.parse(source, filename="skill.py", mode="exec")
        runs = [
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "run"
        ]
        if not runs:
            raise ValueError("Python skill must define run(payload, api).")
        run = runs[0]
        positional = len(run.args.posonlyargs) + len(run.args.args)
        if positional < 2 and run.args.vararg is None:
            raise ValueError("Python skill must define run(payload, api).")

    def run_python(
        skill_dir: Path,
        args: dict[str, Any],
        timeout: float = 8.0,
    ) -> Any:
        try:
            manifest = json.loads(
                (skill_dir / "skill.json").read_text(encoding="utf-8")
            )
        except Exception:
            return original_run_python(skill_dir, args, timeout)
        if not isinstance(manifest, dict) or int(manifest.get("schema_version", 1)) < 2:
            return original_run_python(skill_dir, args, timeout)
        return _run_worker(
            skill_dir,
            args,
            float(manifest.get("runtime_timeout_seconds", 60)),
        )

    def start_approved(self: SkillBuilder, plan: dict[str, Any]) -> dict[str, Any]:
        record = self.tasks.submit(
            "skill_build",
            f"Build approved skill: {plan.get('name', 'New skill')}",
            lambda reporter: _build_approved(self, reporter, plan),
            metadata={
                "suggested_name": plan.get("name", "New skill"),
                "permissions": plan.get("permissions", []),
                "approved": True,
            },
        )
        return {
            "started": True,
            "task_id": record.id,
            "title": record.title,
            "permissions": plan.get("permissions", []),
            "message": "Approved skill creation started in the background.",
        }

    SkillRegistry.validate_manifest = classmethod(validate_manifest)
    SkillBuilder._planner_call = planner_call
    SkillBuilder._builder_call = builder_call
    SkillBuilder._manifest_from_build = manifest_from_build
    SkillBuilder._validate_python_source = staticmethod(validate_python)
    SkillBuilder.start_approved = start_approved
    SkillRuntime.run_python = staticmethod(run_python)
    _PATCHED = True
