from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any

import requests

from .github_client import (
    GitHubRepoClient,
    INDEX_PATH,
    redact_public_text,
    read_suggestion_cache,
    write_suggestion_cache,
)
from .paths import DATA_DIR, SKILLS_DIR

_DISABLED_FILE = DATA_DIR / "disabled_skills.json"


def safe_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not value or not value[0].isalpha():
        value = f"skill_{value}".strip("_")
    return value[:40]


def read_disabled() -> set[str]:
    try:
        value = json.loads(_DISABLED_FILE.read_text(encoding="utf-8"))
        return {str(item) for item in value if isinstance(item, str)}
    except Exception:
        return set()


def write_disabled(values: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = _DISABLED_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")
    temp.replace(_DISABLED_FILE)

class SharedSkillManager:
    def __init__(self, system: Any, logger: Any) -> None:
        self.system = system
        self.logger = logger
        self.client = GitHubRepoClient(logger)
        self.interval = max(
            300, int(os.getenv("SHARED_SKILLS_SYNC_SECONDS", "3600"))
        )
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = threading.Thread(
            target=self._loop, name="JarvisSharedSkillSync", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception:
                self.logger.exception("SHARED SKILLS | sync failed")
            if self._stop.wait(self.interval):
                return

    def _read_index(self) -> dict[str, Any]:
        try:
            value = json.loads(self.client.raw_text(INDEX_PATH))
            return value if isinstance(value, dict) else {"schema_version": 1, "skills": []}
        except Exception:
            return {"schema_version": 1, "skills": []}

    def sync(self) -> dict[str, Any]:
        with self._lock:
            index = self._read_index()
            entries = index.get("skills", [])
            installed: list[str] = []
            errors: list[str] = []
            for entry in entries if isinstance(entries, list) else []:
                try:
                    skill_id = safe_id(str(entry["id"]))
                    files = entry.get("files", [])
                    staging = Path(tempfile.mkdtemp(prefix=f"shared_{skill_id}_"))
                    try:
                        for file_info in files:
                            name = str(file_info["name"])
                            if Path(name).name != name:
                                raise RuntimeError(f"Unsafe shared skill filename: {name}")
                            content = self.client.raw_text(
                                f"jarvis/shared_skills/{skill_id}/{name}"
                            )
                            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                            expected = str(file_info.get("sha256") or "")
                            if expected and digest != expected:
                                raise RuntimeError(f"Hash mismatch for {skill_id}/{name}")
                            (staging / name).write_text(content, encoding="utf-8")

                        manifest = json.loads(
                            (staging / "skill.json").read_text(encoding="utf-8")
                        )
                        from .skill_schema import SkillRegistry
                        SkillRegistry.validate_manifest(manifest, staging)
                        destination = SKILLS_DIR / skill_id
                        backup = SKILLS_DIR / f".{skill_id}.shared_backup"
                        shutil.rmtree(backup, ignore_errors=True)
                        if destination.exists():
                            destination.replace(backup)
                        try:
                            staging.replace(destination)
                        except Exception:
                            if backup.exists() and not destination.exists():
                                backup.replace(destination)
                            raise
                        shutil.rmtree(backup, ignore_errors=True)
                        installed.append(skill_id)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                except Exception as exc:
                    errors.append(f"{entry.get('id', 'unknown')}: {exc}")
                    self.logger.exception("SHARED SKILLS | failed to sync entry")
            self.system.registry.reload()
            self.logger.info(
                "SHARED SKILLS | synced=%d errors=%d", len(installed), len(errors)
            )
            return {"synced": installed, "errors": errors}

    def publish_skill(self, skill_id: str) -> dict[str, Any]:
        if not self.client.write_configured:
            return {
                "published": False,
                "error": "GITHUB_TOKEN is not configured for repository writes.",
            }
        directory = SKILLS_DIR / skill_id
        if not directory.is_dir():
            return {"published": False, "error": "The local skill folder is missing."}
        manifest = json.loads((directory / "skill.json").read_text(encoding="utf-8"))
        filenames = [name for name in ("skill.json", "skill.py") if (directory / name).is_file()]
        files: list[dict[str, str]] = []
        for name in filenames:
            content = (directory / name).read_text(encoding="utf-8")
            self.client.put_text(
                f"jarvis/shared_skills/{skill_id}/{name}",
                content,
                f"Publish shared Jarvis skill {skill_id}: {name}",
            )
            files.append(
                {"name": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            )

        for _ in range(4):
            current_text, index_sha = self.client.get_text(INDEX_PATH)
            index = (
                json.loads(current_text)
                if current_text
                else {"schema_version": 1, "skills": []}
            )
            entries = [
                item for item in index.get("skills", [])
                if str(item.get("id")) != skill_id
            ]
            entries.append(
                {
                    "id": skill_id,
                    "name": manifest["name"],
                    "version": manifest["version"],
                    "description": manifest["description"],
                    "kind": manifest["kind"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "files": files,
                }
            )
            entries.sort(key=lambda item: str(item.get("id")))
            index["skills"] = entries
            try:
                self.client.put_text(
                    INDEX_PATH,
                    json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                    f"Update shared Jarvis skill index for {skill_id}",
                    expected_sha=index_sha,
                    use_expected_sha=True,
                )
                return {"published": True, "skill_id": skill_id}
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 409:
                    raise
        return {"published": False, "error": "GitHub index changed repeatedly."}

    def post_feature_request(
        self,
        *,
        goal: str,
        suggested_name: str,
        reason: str,
        design: str = "",
    ) -> dict[str, Any]:
        if not self.client.write_configured:
            return {
                "posted": False,
                "error": "GITHUB_TOKEN is not configured for issue creation.",
            }
        safe_goal = redact_public_text(goal)
        safe_name = redact_public_text(suggested_name or goal)
        safe_reason = redact_public_text(reason)
        safe_design = redact_public_text(design)
        title = f"[Jarvis capability] {safe_name}"[:240]
        fingerprint = hashlib.sha256(
            f"{title}\n{safe_goal}".encode("utf-8")
        ).hexdigest()
        cache = read_suggestion_cache()
        if fingerprint in cache:
            return {"posted": True, "url": cache[fingerprint], "duplicate": True}
        existing = self.client.find_open_issue(title)
        if existing:
            cache[fingerprint] = existing
            write_suggestion_cache(cache)
            return {"posted": True, "url": existing, "duplicate": True}
        body = (
            "Jarvis could not safely implement this as a generated skill.\n\n"
            f"**Requested capability**\n{safe_goal}\n\n"
            f"**Suggested design**\n{safe_design or 'No design was supplied.'}\n\n"
            f"**Why the skill builder stopped**\n{safe_reason}\n\n"
            "Created automatically by a Jarvis instance for developer review. "
            "Potential secrets and email addresses are redacted before posting."
        )
        url = self.client.create_issue(title, body)
        cache[fingerprint] = url
        write_suggestion_cache(cache)
        return {"posted": True, "url": url, "duplicate": False}

    def set_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        skill_id = safe_id(skill_id)
        disabled = read_disabled()
        if enabled:
            disabled.discard(skill_id)
        else:
            disabled.add(skill_id)
        write_disabled(disabled)
        self.system.registry.reload()
        return {"skill_id": skill_id, "enabled": enabled}

    def list_state(self) -> list[dict[str, Any]]:
        disabled = read_disabled()
        results = []
        for path in sorted(SKILLS_DIR.glob("*/skill.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                results.append(
                    {
                        "id": manifest["id"],
                        "name": manifest["name"],
                        "version": manifest["version"],
                        "enabled": manifest["id"] not in disabled,
                    }
                )
            except Exception:
                continue
        return results


