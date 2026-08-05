from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from jsonschema import Draft202012Validator
from openai import OpenAI

from .settings import Settings
from .skill_schema import SkillDefinition, SkillRegistry, SilentReporter
from .tasks import TaskManager, TaskReporter


class SkillRuntime:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        tasks: TaskManager,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.tasks = tasks

    def invoke(
        self,
        definition: SkillDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        schema = SkillRegistry.parameter_schema(
            list(definition.manifest.get("parameters", []))
        )
        Draft202012Validator(schema).validate(args)

        if bool(definition.manifest.get("background")):
            record = self.tasks.submit(
                "skill_run",
                f"Run skill: {definition.name}",
                lambda reporter: self.run_skill(definition, args, reporter),
                metadata={"skill_id": definition.id, "input": args},
            )
            return {
                "started": True,
                "background": True,
                "task_id": record.id,
                "skill": definition.name,
                "message": "The skill is running in the background.",
            }

        result = self.run_skill(definition, args, SilentReporter())
        return {
            "started": True,
            "background": False,
            "skill": definition.name,
            "result": result,
        }

    def run_skill(
        self,
        definition: SkillDefinition,
        args: dict[str, Any],
        reporter: TaskReporter | SilentReporter,
    ) -> dict[str, Any]:
        manifest = definition.manifest
        if manifest["kind"] == "python":
            reporter.update("Running", "Executing the generated computation.", 0.35)
            value = self.run_python(definition.directory, args)
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str
            )
            return {
                "result": value,
                "result_text": text,
                "spoken_summary": f"{definition.name} finished.",
            }

        client = OpenAI(api_key=self.settings.api_key)
        previous_outputs: list[dict[str, str]] = []
        steps = list(manifest.get("workflow_steps", []))
        for index, step in enumerate(steps, start=1):
            progress = 0.08 + (index - 1) / max(1, len(steps)) * 0.78
            reporter.update(
                str(step["name"]),
                f"Workflow step {index} of {len(steps)} is running.",
                progress,
            )
            prompt = (
                f"Skill: {definition.name}\n"
                f"Skill purpose: {manifest['description']}\n"
                f"User input JSON: {json.dumps(args, ensure_ascii=False)}\n"
                f"Previous step outputs JSON: "
                f"{json.dumps(previous_outputs, ensure_ascii=False)}\n\n"
                f"CURRENT STEP:\n{step['instructions']}\n\n"
                "Return only the useful result for this step. The final step should "
                "produce the answer that will be given to the user."
            )
            tools = [{"type": "web_search"}] if step.get("use_web") else []
            kwargs: dict[str, Any] = {
                "model": self.settings.skill_runtime_model,
                "instructions": (
                    "You are executing a user-created Jarvis skill. Follow the "
                    "skill step precisely, tolerate imperfect transcribed wording, "
                    "and keep intermediate outputs factual and structured."
                ),
                "input": prompt,
                "tools": tools,
                "max_output_tokens": int(step.get("max_output_tokens", 2000)),
                "store": False,
            }
            if self.settings.skill_runtime_model.startswith("gpt-5"):
                kwargs["reasoning"] = {
                    "effort": self.settings.skill_runtime_reasoning
                }
            started = time.perf_counter()
            response = client.responses.create(**kwargs)
            self.logger.info(
                "TIMING | skill %s step %d: %.3fs",
                definition.id,
                index,
                time.perf_counter() - started,
            )
            output = (response.output_text or "").strip()
            if not output:
                raise RuntimeError(f"Workflow step {index} returned no output.")
            previous_outputs.append({"step": str(step["name"]), "output": output})

        final_text = previous_outputs[-1]["output"] if previous_outputs else ""
        reporter.update("Finishing", "Preparing the final skill result.", 0.94)
        return {
            "result_text": final_text,
            "steps": previous_outputs,
            "spoken_summary": f"{definition.name} is finished.",
        }

    @staticmethod
    def run_python(skill_dir: Path, args: dict[str, Any], timeout: float = 8.0) -> Any:
        worker_path = Path(__file__).with_name("skill_worker.py")
        command = [
            sys.executable,
            "-I",
            str(worker_path),
            str(skill_dir),
            json.dumps(args, ensure_ascii=False),
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(process.stderr.strip() or "Skill worker returned no output.")
        payload = json.loads(lines[-1])
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error", "Skill worker failed.")))
        return payload.get("result")

    def test_staged_skill(
        self,
        staging: Path,
        manifest: dict[str, Any],
        reporter: TaskReporter,
    ) -> dict[str, Any]:
        definition = SkillDefinition(staging, manifest)
        tests = list(manifest.get("tests", []))[: self.settings.skill_max_tests]
        if not tests:
            tests = [{"name": "smoke", "input_json": "{}", "expected_contains": []}]
        errors: list[str] = []
        results: list[dict[str, Any]] = []

        for index, test in enumerate(tests, start=1):
            reporter.update(
                "Testing",
                f"Running test {index} of {len(tests)}: {test.get('name', 'test')}",
                0.68 + index / max(1, len(tests)) * 0.18,
            )
            try:
                test_input = json.loads(str(test.get("input_json", "{}")))
                if not isinstance(test_input, dict):
                    raise ValueError("Test input must be a JSON object.")
                schema = SkillRegistry.parameter_schema(
                    list(manifest.get("parameters", []))
                )
                Draft202012Validator(schema).validate(test_input)
                if manifest["kind"] == "workflow":
                    if not self.settings.skill_live_tests:
                        value = {"result_text": "workflow dry-run passed"}
                    else:
                        value = self.run_skill(definition, test_input, SilentReporter())
                else:
                    raw = self.run_python(staging, test_input)
                    value = {"result_text": raw}
                result_text = str(value.get("result_text", value))
                missing = [
                    expected
                    for expected in test.get("expected_contains", [])
                    if expected.lower() not in result_text.lower()
                ]
                if not result_text.strip():
                    raise AssertionError("The skill returned an empty result.")
                if missing:
                    raise AssertionError(f"Missing expected text: {missing}")
                results.append({"name": test.get("name"), "passed": True})
            except Exception as exc:
                errors.append(f"{test.get('name', 'test')}: {type(exc).__name__}: {exc}")
                results.append(
                    {"name": test.get("name"), "passed": False, "error": str(exc)}
                )

        return {"passed": not errors, "tests": results, "errors": errors}
