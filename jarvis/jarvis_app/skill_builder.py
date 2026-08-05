from __future__ import annotations

from datetime import datetime, timezone
import ast
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

from openai import OpenAI

from .paths import SKILLS_DIR, SKILL_STAGING_DIR
from .settings import Settings
from .skill_runtime import SkillRuntime
from .skill_schema import BUILD_SCHEMA, PLAN_SCHEMA, SkillRegistry
from .tasks import TaskManager, TaskReporter


class SkillBuilder:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        tasks: TaskManager,
        registry: SkillRegistry,
        runtime: SkillRuntime,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.tasks = tasks
        self.registry = registry
        self.runtime = runtime
        SKILL_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    def start(self, goal: str, name: str, design: str) -> dict[str, Any]:
        record = self.tasks.submit(
            "skill_build",
            f"Build skill: {name}",
            lambda reporter: self._build_worker(reporter, goal, name, design),
            metadata={"goal": goal, "suggested_name": name, "design": design},
        )
        return {
            "started": True,
            "task_id": record.id,
            "title": record.title,
            "message": "Skill creation started in the background.",
        }

    @staticmethod
    def _structured_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": {
                "type": "json_schema",
                "name": name,
                "schema": schema,
                "strict": True,
            },
            "verbosity": "low",
        }

    def _model_response(
        self,
        *,
        role: str,
        model: str,
        reasoning: str,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
    ) -> dict[str, Any]:
        client = OpenAI(api_key=self.settings.api_key)
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "text": self._structured_format(schema_name, schema),
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if model.startswith("gpt-5"):
            kwargs["reasoning"] = {"effort": reasoning, "summary": "concise"}

        started = time.perf_counter()
        try:
            response = client.responses.create(**kwargs)
        except Exception as exc:
            message = str(exc).lower()
            fallback = self.settings.text_model
            if model != fallback and any(
                marker in message
                for marker in (
                    "model_not_found", "does not exist", "not found",
                    "not have access", "unsupported model"
                )
            ):
                self.logger.warning(
                    "SKILLS | %s model %s unavailable; falling back to %s",
                    role, model, fallback,
                )
                kwargs["model"] = fallback
                kwargs.pop("reasoning", None)
                response = client.responses.create(**kwargs)
            else:
                raise
        self.logger.info(
            "TIMING | skill %s model call: %.3fs | model=%s",
            role,
            time.perf_counter() - started,
            getattr(response, "model", model),
        )
        text = (response.output_text or "").strip()
        if not text:
            raise RuntimeError(f"{role} returned no structured output.")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RuntimeError(f"{role} output was not a JSON object.")
        return data

    def _planner_call(self, goal: str, name: str, design: str) -> dict[str, Any]:
        instructions = """
You are Sol, the high-reasoning architect for Jarvis's self-created skill system.
Design a small, maintainable, testable skill that fits Jarvis's existing quality.

Available safe skill types:
1. workflow: one or more model steps, optionally with OpenAI web search. Use this
   for research, analysis, writing, monitoring logic, and tasks needing current data.
2. python: deterministic pure computation. Generated code receives a JSON object
   in run(payload), may not import modules, access files, network, processes, OS,
   environment variables, devices, credentials, or private attributes. Safe math,
   statistics, regex, JSON, and datetime modules are preloaded.

Available permissions are only model and web_search. Skills cannot modify Jarvis
core files, execute shell commands, install packages, control arbitrary software,
or access unrestricted files. Mark buildable=false when the requested capability
cannot be implemented safely with these interfaces. Prefer workflow skills for
anything remotely open-ended. Workflow skills run in the background.

Produce a detailed implementation plan and realistic smoke tests. Parameter names
must be lowercase snake_case. IDs must be lowercase snake_case, 3-40 characters.
""".strip()
        return self._model_response(
            role=self.settings.skill_planner_alias,
            model=self.settings.skill_planner_model,
            reasoning=self.settings.skill_planner_reasoning,
            instructions=instructions,
            input_text=(
                f"Requested capability: {goal}\n"
                f"Suggested name: {name}\n"
                f"Initial design idea: {design or 'none'}"
            ),
            schema_name="jarvis_skill_plan",
            schema=PLAN_SCHEMA,
            max_output_tokens=6000,
        )

    def _builder_call(
        self,
        plan: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        instructions = """
You are Luna, the low-reasoning implementation and test engineer for Jarvis skills.
Follow the architect's plan exactly and produce a compact, dependable implementation.

For workflow skills, write precise step instructions that include the user input,
use prior step outputs, avoid unnecessary verbosity, and return a useful final answer.
For Python skills, output code with a required run(payload) function. Do not use any
imports, dunder names, file access, networking, subprocesses, eval, exec, reflection,
or global state. Only ordinary Python plus preloaded math, statistics, re, json, and
datetime are available.

Tests must use valid JSON input objects. expected_contains may be empty when the
important assertion is simply that a non-empty result is produced. Address every
piece of repair feedback when provided.
""".strip()
        return self._model_response(
            role=self.settings.skill_builder_alias,
            model=self.settings.skill_builder_model,
            reasoning=self.settings.skill_builder_reasoning,
            instructions=instructions,
            input_text=(
                "ARCHITECT PLAN:\n"
                + json.dumps(plan, indent=2, ensure_ascii=False)
                + "\n\nREPAIR FEEDBACK:\n"
                + (feedback or "No previous implementation errors.")
            ),
            schema_name="jarvis_skill_build",
            schema=BUILD_SCHEMA,
            max_output_tokens=8000,
        )

    @staticmethod
    def _normalise_id(value: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        if not value or not value[0].isalpha():
            value = f"skill_{value}".strip("_")
        return value[:40]

    def _manifest_from_build(
        self,
        plan: dict[str, Any],
        build: dict[str, Any],
    ) -> dict[str, Any]:
        skill_id = self._normalise_id(str(plan["id"]))
        kind = str(plan["kind"])
        permissions = list(dict.fromkeys(plan.get("permissions", [])))
        if kind == "workflow" and "model" not in permissions:
            permissions.append("model")
        return {
            "schema_version": 1,
            "id": skill_id,
            "name": str(plan["name"]).strip(),
            "version": "1.0.0",
            "description": str(plan["description"]).strip(),
            "kind": kind,
            "background": bool(plan["background"] or kind == "workflow"),
            "parameters": plan.get("parameters", []),
            "permissions": permissions,
            "workflow_steps": (
                build.get("workflow_steps", []) if kind == "workflow" else []
            ),
            "tests": build.get("tests", []),
            "build": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "planner_alias": self.settings.skill_planner_alias,
                "planner_model": self.settings.skill_planner_model,
                "planner_reasoning": self.settings.skill_planner_reasoning,
                "builder_alias": self.settings.skill_builder_alias,
                "builder_model": self.settings.skill_builder_model,
                "builder_reasoning": self.settings.skill_builder_reasoning,
                "implementation_summary": build.get("implementation_summary", ""),
                "self_review": build.get("self_review", ""),
            },
        }

    def _write_staging(
        self,
        plan: dict[str, Any],
        build: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        manifest = self._manifest_from_build(plan, build)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f"{manifest['id']}_",
                dir=SKILL_STAGING_DIR,
            )
        )
        (staging / "skill.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (staging / "plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if manifest["kind"] == "python":
            (staging / "skill.py").write_text(
                str(build.get("python_code", "")), encoding="utf-8"
            )
        SkillRegistry.validate_manifest(manifest, staging)
        if manifest["kind"] == "python":
            self._validate_python_source(
                (staging / "skill.py").read_text(encoding="utf-8")
            )
        return staging, manifest

    @staticmethod
    def _validate_python_source(source: str) -> None:
        tree = ast.parse(source, filename="skill.py", mode="exec")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                raise ValueError("Generated Python skill uses a forbidden statement.")
            if isinstance(node, ast.Attribute) and (
                node.attr.startswith("_") or "__" in node.attr
            ):
                raise ValueError("Generated Python skill accesses a private attribute.")
            if isinstance(node, ast.Name) and "__" in node.id:
                raise ValueError("Generated Python skill uses a dunder name.")

    def _build_worker(
        self,
        reporter: TaskReporter,
        goal: str,
        name: str,
        design: str,
    ) -> dict[str, Any]:
        reporter.update(
            "Planning",
            f"{self.settings.skill_planner_alias} is making the detailed plan.",
            0.08,
        )
        plan = self._planner_call(goal, name, design)
        if not bool(plan.get("buildable")):
            reason = str(plan.get("block_reason") or "The skill needs unsafe access.")
            return {
                "installed": False,
                "summary": reason,
                "spoken_summary": f"I couldn't build it safely: {reason}",
                "plan": plan,
            }

        reporter.update(
            "Programming",
            f"The plan is finished. {self.settings.skill_builder_alias} is programming it.",
            0.32,
        )
        feedback = ""
        last_errors: list[str] = []
        for attempt in range(self.settings.skill_build_retries + 1):
            build = self._builder_call(plan, feedback)
            staging: Path | None = None
            try:
                reporter.update(
                    "Validating",
                    "Checking the manifest, permissions, schemas, and code safety.",
                    0.52,
                )
                staging, manifest = self._write_staging(plan, build)
                reporter.update(
                    "Testing",
                    f"{self.settings.skill_builder_alias} is running the generated tests.",
                    0.68,
                )
                test_report = self.runtime.test_staged_skill(
                    staging, manifest, reporter
                )
                if not test_report["passed"]:
                    raise RuntimeError("; ".join(test_report["errors"]))

                reporter.update(
                    "Installing",
                    "Tests passed. Installing the skill atomically.",
                    0.92,
                )
                destination = self._install_staging(staging, manifest)
                staging = None
                self.registry.reload()
                summary = f"{manifest['name']} is built, tested, and ready."
                return {
                    "installed": True,
                    "skill_id": manifest["id"],
                    "skill_name": manifest["name"],
                    "path": str(destination),
                    "test_report": test_report,
                    "plan": plan,
                    "spoken_summary": summary,
                    "summary": summary,
                }
            except Exception as exc:
                last_errors.append(
                    f"Attempt {attempt + 1}: {type(exc).__name__}: {exc}"
                )
                self.logger.exception("SKILLS | build attempt %d failed", attempt + 1)
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                if attempt >= self.settings.skill_build_retries:
                    raise RuntimeError(" | ".join(last_errors)) from exc
                feedback = (
                    "The previous implementation failed validation or tests. "
                    "Repair all of these errors:\n" + "\n".join(last_errors)
                )
                reporter.update(
                    "Repairing",
                    f"A test failed. {self.settings.skill_builder_alias} is repairing it.",
                    0.58,
                )
        raise RuntimeError("Skill build ended unexpectedly.")

    @staticmethod
    def _install_staging(staging: Path, manifest: dict[str, Any]) -> Path:
        destination = SKILLS_DIR / str(manifest["id"])
        backup = SKILLS_DIR / f".{manifest['id']}.backup"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if destination.exists():
            destination.replace(backup)
        try:
            staging.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        else:
            shutil.rmtree(backup, ignore_errors=True)
        return destination
