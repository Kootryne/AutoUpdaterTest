from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from .paths import DATA_DIR, SKILLS_DIR, SKILL_STAGING_DIR
from .privileged_skill_api import (
    ALL_PERMISSIONS,
    PERMISSION_SPECS,
    permission_labels,
    permission_risks,
)


_PATCHED = False
_PENDING = DATA_DIR / "pending_skill_edit_v095.json"
_REVISION_ROOT = DATA_DIR / "skill_revisions"
_MAX_REVISIONS = 5


def _write_pending(value: dict[str, Any]) -> None:
    _PENDING.parent.mkdir(parents=True, exist_ok=True)
    temporary = _PENDING.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(_PENDING)


def _read_pending() -> dict[str, Any] | None:
    try:
        value = json.loads(_PENDING.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _clear_pending() -> None:
    _PENDING.unlink(missing_ok=True)


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _resolve_definition(system: Any, value: str) -> Any | None:
    wanted = _safe_name(value)
    lowered = value.strip().lower()
    exact: list[Any] = []
    fuzzy: list[tuple[float, int, Any]] = []
    wanted_tokens = {part for part in wanted.split("_") if part}

    for definition in system.registry.skills.values():
        candidates = {
            definition.id.lower(),
            _safe_name(definition.id),
            definition.name.lower(),
            _safe_name(definition.name),
        }
        if lowered in candidates or wanted in candidates:
            exact.append(definition)
            continue

        candidate_tokens: set[str] = set()
        for candidate in candidates:
            candidate_tokens.update(
                part for part in _safe_name(candidate).split("_") if part
            )
        overlap = len(wanted_tokens & candidate_tokens)
        if wanted_tokens and overlap:
            fuzzy.append((overlap / len(wanted_tokens), overlap, definition))

    if exact:
        return exact[0]
    fuzzy.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if fuzzy and fuzzy[0][0] >= 0.5:
        if len(fuzzy) == 1 or fuzzy[0][:2] > fuzzy[1][:2]:
            return fuzzy[0][2]
    return None


def _source_snapshot(definition: Any) -> dict[str, Any]:
    directory = Path(definition.directory)
    manifest = dict(definition.manifest)
    source = ""
    source_name: str | None = None

    if manifest.get("kind") == "python":
        source_name = "skill.py"
        source_path = directory / source_name
        if source_path.exists():
            source = source_path.read_text(encoding="utf-8")
    else:
        source_name = "workflow_steps"
        source = json.dumps(
            manifest.get("workflow_steps", []),
            ensure_ascii=False,
            indent=2,
        )

    previous_plan = None
    plan_path = directory / "plan.json"
    if plan_path.exists():
        try:
            value = json.loads(plan_path.read_text(encoding="utf-8"))
            previous_plan = value if isinstance(value, dict) else None
        except Exception:
            previous_plan = None

    digest = hashlib.sha256()
    digest.update(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    digest.update(source.encode("utf-8"))
    return {
        "skill_id": definition.id,
        "skill_name": definition.name,
        "directory": str(directory),
        "manifest": manifest,
        "source_name": source_name,
        "source": source[:50000],
        "previous_plan": previous_plan,
        "content_hash": digest.hexdigest(),
    }


def _current_hash(definition: Any) -> str:
    return str(_source_snapshot(definition)["content_hash"])


def _bump_patch(version: str) -> str:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", str(version))
    if not match:
        return "1.0.1"
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _backup_existing(manifest: dict[str, Any], request: str) -> Path | None:
    skill_id = str(manifest["id"])
    current = SKILLS_DIR / skill_id
    if not current.exists():
        return None

    root = _REVISION_ROOT / skill_id
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    old_version = str(
        manifest.get("build", {}).get("previous_version") or "unknown"
    )
    destination = root / f"{stamp}_{_safe_name(old_version) or 'unknown'}"
    suffix = 2
    while destination.exists():
        destination = root / (
            f"{stamp}_{_safe_name(old_version) or 'unknown'}_{suffix}"
        )
        suffix += 1

    shutil.copytree(current, destination)
    (destination / "revision.json").write_text(
        json.dumps(
            {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "replaced_by_version": manifest.get("version"),
                "edit_request": request,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    revisions = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for old in revisions[_MAX_REVISIONS:]:
        shutil.rmtree(old, ignore_errors=True)
    return destination


def _edit_worker(builder: Any, reporter: Any, plan: dict[str, Any]) -> dict[str, Any]:
    context = dict(plan.get("_edit_context") or {})
    skill_id = str(context["existing_id"])
    request = str(context.get("requested_change") or "")

    reporter.update(
        "Programming",
        f"{builder.settings.skill_builder_alias} is revising "
        f"{plan.get('name', skill_id)}.",
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
                "Checking the revised implementation, permissions, and version.",
                0.50,
            )
            staging, manifest = builder._write_staging(plan, build)
            if str(manifest["id"]) != skill_id:
                raise RuntimeError("A skill edit cannot change the skill ID.")

            reporter.update(
                "Testing",
                "Running new tests and regression tests before replacement.",
                0.68,
            )
            report = builder.runtime.test_staged_skill(staging, manifest, reporter)
            if not report["passed"]:
                raise RuntimeError("; ".join(report["errors"]))

            reporter.update(
                "Installing",
                "Saving the previous revision and replacing the skill atomically.",
                0.92,
            )
            backup = _backup_existing(manifest, request)
            destination = builder._install_staging(staging, manifest)
            staging = None
            builder.registry.reload()
            summary = (
                f"{manifest['name']} was updated to version "
                f"{manifest['version']} and passed its tests."
            )
            return {
                "installed": True,
                "edited": True,
                "skill_id": manifest["id"],
                "skill_name": manifest["name"],
                "version": manifest["version"],
                "path": str(destination),
                "backup": str(backup) if backup else None,
                "permissions": manifest.get("permissions", []),
                "test_report": report,
                "summary": summary,
                "spoken_summary": summary,
            }
        except Exception as exc:
            errors.append(f"Attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            builder.logger.exception(
                "SKILLS | edit attempt %d failed | skill=%s",
                attempt + 1,
                skill_id,
            )
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if attempt >= builder.settings.skill_build_retries:
                raise RuntimeError(" | ".join(errors)) from exc
            feedback = (
                "Repair every failure below while preserving the requested edit "
                "and approved permission set:\n" + "\n".join(errors)
            )
            reporter.update(
                "Repairing",
                f"{builder.settings.skill_builder_alias} is repairing the revision.",
                0.58,
            )

    raise RuntimeError("Skill edit ended unexpectedly.")


def _revision_list(skill_id: str) -> list[dict[str, Any]]:
    root = _REVISION_ROOT / skill_id
    if not root.exists():
        return []

    result: list[dict[str, Any]] = []
    for directory in sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        manifest: dict[str, Any] = {}
        try:
            value = json.loads(
                (directory / "skill.json").read_text(encoding="utf-8")
            )
            manifest = value if isinstance(value, dict) else {}
        except Exception:
            pass
        result.append(
            {
                "revision": directory.name,
                "version": manifest.get("version"),
                "name": manifest.get("name"),
                "path": str(directory),
            }
        )
    return result


def _restore_revision(
    system: Any,
    skill_value: str,
    revision: str | None,
) -> dict[str, Any]:
    definition = _resolve_definition(system, skill_value)
    skill_id = definition.id if definition is not None else _safe_name(skill_value)
    revisions = _revision_list(skill_id)
    if not revisions:
        return {"restored": False, "error": "No saved revisions were found."}

    selected = None
    if revision:
        selected = next(
            (item for item in revisions if item["revision"] == revision),
            None,
        )
    else:
        selected = revisions[0]
    if selected is None:
        return {"restored": False, "error": "That revision was not found."}

    source = Path(str(selected["path"]))
    manifest = json.loads((source / "skill.json").read_text(encoding="utf-8"))
    system.registry.validate_manifest(manifest, source)

    temporary = SKILL_STAGING_DIR / (
        f"rollback_{skill_id}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    if temporary.exists():
        shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(source, temporary)
    (temporary / "revision.json").unlink(missing_ok=True)

    current_version = (
        definition.manifest.get("version") if definition is not None else None
    )
    backup_manifest = {
        **manifest,
        "build": {
            **dict(manifest.get("build") or {}),
            "previous_version": current_version,
        },
    }
    _backup_existing(
        backup_manifest,
        f"Rollback to {selected['revision']}",
    )
    destination = system.builder._install_staging(temporary, manifest)
    system.registry.reload()
    return {
        "restored": True,
        "skill_id": skill_id,
        "version": manifest.get("version"),
        "revision": selected["revision"],
        "path": str(destination),
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .skill_builder import SkillBuilder
    from .skill_schema import BUILD_SCHEMA, PLAN_SCHEMA
    from .skills import SkillSystem

    original_builder_call = SkillBuilder._builder_call
    original_manifest_from_build = SkillBuilder._manifest_from_build
    original_schemas = SkillSystem.schemas
    original_call = SkillSystem.call
    original_handles = SkillSystem.handles_tool
    original_context = SkillSystem.prompt_context
    original_instructions = Brain.instructions

    def edit_planner_call(
        self: SkillBuilder,
        snapshot: dict[str, Any],
        requested_change: str,
        issue_or_context: str,
    ) -> dict[str, Any]:
        permissions = "\n".join(
            f"- {key}: {spec['label']}. {spec['risk']}"
            for key, spec in PERMISSION_SPECS.items()
        )
        instructions = f"""
You are Sol, Jarvis's high-reasoning skill revision architect. Revise an
existing installed skill instead of designing a separate replacement. Preserve
its exact ID. Keep current permissions unless the requested behavior genuinely
needs new ones; remove unused permissions when safe. Return a full replacement
plan.

Permissions:
{permissions}

Inspect the existing manifest, implementation, previous tests, requested change,
and issue or debugging context. Add regression tests for behavior that must not
trigger, positive tests for requested behavior, and stable output-contract
assertions. For camera or screen skills, access visual devices only when the
user's request requires visual information. Do not reject ordinary supported
permissions.
""".strip()
        plan = self._model_response(
            role=self.settings.skill_planner_alias,
            model=self.settings.skill_planner_model,
            reasoning=self.settings.skill_planner_reasoning,
            instructions=instructions,
            input_text=(
                "EXISTING SKILL:\n"
                + json.dumps(snapshot, ensure_ascii=False, indent=2)
                + "\n\nREQUESTED EDIT:\n"
                + requested_change
                + "\n\nISSUE OR DEBUG CONTEXT:\n"
                + (issue_or_context or "None.")
            ),
            schema_name="jarvis_skill_revision_plan",
            schema=PLAN_SCHEMA,
            max_output_tokens=8500,
        )
        plan["id"] = snapshot["skill_id"]
        return plan

    def builder_call(
        self: SkillBuilder,
        plan: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        if not plan.get("_edit_context"):
            return original_builder_call(self, plan, feedback)

        context = dict(plan.get("_edit_context") or {})
        instructions = """
You are Luna, Jarvis's skill revision engineer. Produce a complete replacement
implementation for the installed skill, not a patch or explanation. Follow the
approved edit plan exactly. Preserve every existing behavior that the requested
change does not intentionally alter. Never add permissions beyond the plan.

For Python skills define run(payload, api), use no imports, and use only the
permission-checked SkillAPI. Validate trigger conditions before accessing cameras,
screens, files, programs, or input devices. Return stable JSON-serializable result
contracts. Tests must include regressions for ordinary non-triggering input and
positive tests for the requested behavior. Address every repair error.
""".strip()
        existing = {
            "manifest": context.get("existing_manifest"),
            "source_name": context.get("source_name"),
            "source": context.get("existing_source"),
            "previous_plan": context.get("previous_plan"),
        }
        return self._model_response(
            role=self.settings.skill_builder_alias,
            model=self.settings.skill_builder_model,
            reasoning=self.settings.skill_builder_reasoning,
            instructions=instructions,
            input_text=(
                "APPROVED EDIT PLAN:\n"
                + json.dumps(plan, ensure_ascii=False, indent=2)
                + "\n\nEXISTING INSTALLED SKILL:\n"
                + json.dumps(existing, ensure_ascii=False, indent=2)
                + "\n\nREPAIR FEEDBACK:\n"
                + (feedback or "None.")
            ),
            schema_name="jarvis_skill_revision_build",
            schema=BUILD_SCHEMA,
            max_output_tokens=12000,
        )

    def manifest_from_build(
        self: SkillBuilder,
        plan: dict[str, Any],
        build: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = original_manifest_from_build(self, plan, build)
        context = plan.get("_edit_context")
        if not isinstance(context, dict):
            return manifest

        existing = dict(context.get("existing_manifest") or {})
        manifest["id"] = str(context["existing_id"])
        manifest["version"] = _bump_patch(str(existing.get("version", "1.0.0")))
        manifest["build"] = {
            **dict(manifest.get("build") or {}),
            "edited_at": datetime.now(timezone.utc).isoformat(),
            "previous_version": existing.get("version"),
            "edit_request": context.get("requested_change"),
            "edit_context": context.get("issue_or_context"),
        }
        return manifest

    def start_approved_edit(
        self: SkillBuilder,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        record = self.tasks.submit(
            "skill_edit",
            f"Edit skill: {plan.get('name', plan.get('id', 'skill'))}",
            lambda reporter: _edit_worker(self, reporter, plan),
            metadata={
                "skill_id": plan.get("id"),
                "permissions": plan.get("permissions", []),
                "edit": True,
                "requested_change": dict(plan.get("_edit_context") or {}).get(
                    "requested_change"
                ),
            },
        )
        return {
            "started": True,
            "task_id": record.id,
            "title": record.title,
            "skill_id": plan.get("id"),
            "permissions": plan.get("permissions", []),
            "message": "Skill editing started in the background.",
        }

    def prepare_edit(
        self: SkillSystem,
        *,
        skill_id_or_name: str,
        requested_change: str,
        issue_or_context: str,
    ) -> dict[str, Any]:
        if _read_pending():
            return {
                "started": False,
                "approval_required": True,
                "error": "Another skill edit is awaiting permission approval.",
            }

        try:
            from . import skill_approval_v092

            if skill_approval_v092._read():
                return {
                    "started": False,
                    "approval_required": True,
                    "error": "A new skill build is already awaiting approval.",
                }
        except Exception:
            pass

        definition = _resolve_definition(self, skill_id_or_name)
        if definition is None:
            return {
                "started": False,
                "found": False,
                "error": "No installed skill matched that ID or name.",
                "installed": [
                    {"id": item.id, "name": item.name}
                    for item in self.registry.skills.values()
                ],
            }

        change = requested_change.strip()
        if not change:
            return {"started": False, "error": "No requested change was provided."}

        snapshot = _source_snapshot(definition)
        plan = self.builder._edit_planner_call(
            snapshot,
            change,
            issue_or_context.strip(),
        )
        if not bool(plan.get("buildable")):
            return {
                "started": False,
                "approval_required": False,
                "error": str(
                    plan.get("block_reason")
                    or "The requested skill edit could not be planned."
                ),
                "plan": plan,
            }

        permissions = list(
            dict.fromkeys(str(value) for value in plan.get("permissions", []))
        )
        if plan.get("kind") == "workflow" and "model" not in permissions:
            permissions.insert(0, "model")
        unknown = set(permissions) - set(ALL_PERMISSIONS)
        if unknown:
            raise ValueError(
                f"Planner requested unknown permissions: {sorted(unknown)}"
            )

        old_permissions = {
            str(value) for value in definition.manifest.get("permissions", [])
        }
        added = [value for value in permissions if value not in old_permissions]
        plan["permissions"] = permissions
        plan["_edit_context"] = {
            "existing_id": definition.id,
            "existing_version": definition.manifest.get("version"),
            "existing_hash": snapshot["content_hash"],
            "existing_manifest": snapshot["manifest"],
            "existing_source": snapshot["source"],
            "source_name": snapshot["source_name"],
            "previous_plan": snapshot["previous_plan"],
            "requested_change": change,
            "issue_or_context": issue_or_context.strip(),
            "old_permissions": sorted(old_permissions),
        }

        if added:
            pending = {
                "proposal_id": uuid4().hex[:12],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mode": "edit",
                "skill_id": definition.id,
                "skill_name": definition.name,
                "existing_version": definition.manifest.get("version"),
                "existing_hash": snapshot["content_hash"],
                "plan": plan,
                "permissions": permissions,
                "added_permissions": added,
                "permission_labels": permission_labels(added),
                "risks": permission_risks(added),
                "status": "awaiting_approval",
            }
            _write_pending(pending)
            return {
                "started": False,
                "approval_required": True,
                "proposal_id": pending["proposal_id"],
                "skill_id": definition.id,
                "skill_name": definition.name,
                "existing_permissions": sorted(old_permissions),
                "new_permissions": permissions,
                "added_permissions": added,
                "added_permission_labels": pending["permission_labels"],
                "risks": pending["risks"],
                "instruction": (
                    "Explain only the newly added permissions and risks, then ask "
                    "one explicit yes/no question. Existing permissions were "
                    "already approved."
                ),
            }

        plan["_approval"] = {
            "proposal_id": f"edit-{uuid4().hex[:12]}",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "permissions": permissions,
            "risks": list(
                definition.manifest.get("approval", {}).get(
                    "risks_acknowledged", []
                )
            ),
        }
        result = self.builder.start_approved_edit(plan)
        return {
            **result,
            "approval_required": False,
            "skill_name": definition.name,
            "previous_version": definition.manifest.get("version"),
        }

    def approve_edit(self: SkillSystem) -> dict[str, Any]:
        pending = _read_pending()
        if not pending or pending.get("mode") != "edit":
            return {
                "started": False,
                "approved": False,
                "error": "There is no skill edit awaiting approval.",
            }

        definition = self.registry.skills.get(str(pending.get("skill_id")))
        if definition is None:
            _clear_pending()
            return {
                "started": False,
                "approved": False,
                "error": "The skill is no longer installed.",
            }
        if (
            str(definition.manifest.get("version"))
            != str(pending.get("existing_version"))
            or _current_hash(definition) != str(pending.get("existing_hash"))
        ):
            _clear_pending()
            return {
                "started": False,
                "approved": False,
                "error": (
                    "The installed skill changed while approval was pending. "
                    "Ask Jarvis to plan the edit again."
                ),
            }

        plan = pending.get("plan")
        if not isinstance(plan, dict):
            _clear_pending()
            return {
                "started": False,
                "approved": False,
                "error": "The pending edit plan is invalid.",
            }

        permissions = list(pending.get("permissions", []))
        prior_risks = list(
            definition.manifest.get("approval", {}).get(
                "risks_acknowledged", []
            )
        )
        plan["_approval"] = {
            "proposal_id": str(pending["proposal_id"]),
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "permissions": permissions,
            "risks": list(dict.fromkeys(prior_risks + list(pending.get("risks", [])))),
        }
        result = self.builder.start_approved_edit(plan)
        _clear_pending()
        return {
            **result,
            "approved": True,
            "skill_name": pending.get("skill_name"),
        }

    def schemas(self: SkillSystem) -> list[dict[str, Any]]:
        result = [
            schema
            for schema in original_schemas(self)
            if schema.get("name")
            not in {
                "edit_installed_skill",
                "approve_skill_edit",
                "cancel_skill_edit",
                "get_pending_skill_edit",
                "list_skill_revisions",
                "rollback_skill_edit",
            }
        ]
        result.extend(
            [
                {
                    "type": "function",
                    "name": "edit_installed_skill",
                    "description": (
                        "Plan and apply a targeted edit to an installed skill. "
                        "Unchanged permissions need no additional confirmation; "
                        "new permissions require one explicit risk approval."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id_or_name": {"type": "string"},
                            "requested_change": {"type": "string"},
                            "issue_or_context": {"type": ["string", "null"]},
                        },
                        "required": [
                            "skill_id_or_name",
                            "requested_change",
                            "issue_or_context",
                        ],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "approve_skill_edit",
                    "description": (
                        "Approve a pending skill edit only after the user accepts "
                        "its newly added permissions and risks."
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
                    "name": "cancel_skill_edit",
                    "description": "Cancel the pending skill edit plan.",
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
                    "name": "get_pending_skill_edit",
                    "description": "Inspect a skill edit awaiting permission approval.",
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
                    "name": "list_skill_revisions",
                    "description": "List saved previous revisions of a skill.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id_or_name": {"type": "string"},
                        },
                        "required": ["skill_id_or_name"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "rollback_skill_edit",
                    "description": (
                        "Restore a saved previous skill revision. Omit revision to "
                        "restore the newest backup."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id_or_name": {"type": "string"},
                            "revision": {"type": ["string", "null"]},
                        },
                        "required": ["skill_id_or_name", "revision"],
                        "additionalProperties": False,
                    },
                },
            ]
        )
        return result

    def call(
        self: SkillSystem,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "edit_installed_skill":
            return self.prepare_skill_edit(
                skill_id_or_name=str(args["skill_id_or_name"]),
                requested_change=str(args["requested_change"]),
                issue_or_context=str(args.get("issue_or_context") or ""),
            )
        if name == "approve_skill_edit":
            return self.approve_pending_skill_edit()
        if name == "cancel_skill_edit":
            pending = _read_pending()
            _clear_pending()
            return {
                "cancelled": pending is not None,
                "skill_id": pending.get("skill_id") if pending else None,
            }
        if name == "get_pending_skill_edit":
            pending = _read_pending()
            return {
                "pending": bool(pending),
                "proposal": (
                    {
                        key: pending.get(key)
                        for key in (
                            "proposal_id",
                            "skill_id",
                            "skill_name",
                            "existing_version",
                            "added_permissions",
                            "permission_labels",
                            "risks",
                        )
                    }
                    if pending
                    else None
                ),
            }
        if name == "list_skill_revisions":
            definition = _resolve_definition(self, str(args["skill_id_or_name"]))
            skill_id = (
                definition.id
                if definition is not None
                else _safe_name(str(args["skill_id_or_name"]))
            )
            return {"skill_id": skill_id, "revisions": _revision_list(skill_id)}
        if name == "rollback_skill_edit":
            return _restore_revision(
                self,
                str(args["skill_id_or_name"]),
                str(args.get("revision")) if args.get("revision") else None,
            )
        return original_call(self, name, args)

    def handles_tool(self: SkillSystem, name: str) -> bool:
        if name in {
            "edit_installed_skill",
            "approve_skill_edit",
            "cancel_skill_edit",
            "get_pending_skill_edit",
            "list_skill_revisions",
            "rollback_skill_edit",
        }:
            return True
        return original_handles(self, name)

    def prompt_context(self: SkillSystem) -> str:
        pending = _read_pending()
        compact = (
            {
                key: pending.get(key)
                for key in (
                    "proposal_id",
                    "skill_id",
                    "skill_name",
                    "existing_version",
                    "added_permissions",
                    "permission_labels",
                    "risks",
                )
            }
            if pending
            else None
        )
        return (
            f"{original_context(self)}\n\nPENDING SKILL EDIT:\n"
            f"{json.dumps(compact, ensure_ascii=False)}"
        )

    def instructions(self: Brain) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "EDITING INSTALLED SKILLS\n"
            "- Use edit_installed_skill when the user asks to fix, change, improve, "
            "extend, or repair an installed skill. Do not build a duplicate skill.\n"
            "- Include concrete issue text, failing test details, or session-log "
            "evidence in issue_or_context when available.\n"
            "- If no permissions are added, the explicit edit request is enough and "
            "the edit starts after planning. If permissions are added, explain only "
            "the new risks and ask once; on a later yes use approve_skill_edit.\n"
            "- Previous revisions are saved locally. Use list_skill_revisions and "
            "rollback_skill_edit when the user asks to undo an edit.\n"
            "- Never claim an edit succeeded until its background task reports "
            "success.\n"
        )

    SkillBuilder._edit_planner_call = edit_planner_call
    SkillBuilder.start_approved_edit = start_approved_edit
    SkillBuilder._builder_call = builder_call
    SkillBuilder._manifest_from_build = manifest_from_build
    SkillSystem.prepare_skill_edit = prepare_edit
    SkillSystem.approve_pending_skill_edit = approve_edit
    SkillSystem.schemas = schemas
    SkillSystem.call = call
    SkillSystem.handles_tool = handles_tool
    SkillSystem.prompt_context = prompt_context
    Brain.instructions = instructions
    _PATCHED = True
