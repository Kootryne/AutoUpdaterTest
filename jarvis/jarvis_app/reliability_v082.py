from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any

import numpy as np

from . import language_mode
from .language_mode import detect_language as _base_detect_language
from .paths import DATA_DIR, SKILLS_DIR
from .shared_skill_manager import (
    SharedSkillManager,
    read_disabled,
    safe_id,
    write_disabled,
)

_PATCHED = False

_SV_STRONG = {
    "hej", "jag", "du", "dig", "din", "ditt", "kan", "har", "är", "inte",
    "vad", "hur", "varför", "igen", "kolla", "kontrollera", "skillen",
    "skärmen", "kameran", "tog", "bort", "bara", "gömde", "stängde",
}
_EN_STRONG = {
    "hey", "i", "you", "your", "can", "have", "are", "not", "what", "how",
    "why", "again", "check", "checking", "try", "skill", "screen", "camera",
    "webcam", "remove", "delete", "disable", "enable",
}


def detect_language(text: str, previous: str = "en") -> str:
    normalized = text.lower().strip()
    words = re.findall(r"[a-zåäö']+", normalized)
    sv = sum(word in _SV_STRONG for word in words)
    en = sum(word in _EN_STRONG for word in words)
    sv += 2 * sum(normalized.count(char) for char in "åäö")

    if re.match(r"^\s*hej\b", normalized):
        sv += 4
    if re.match(r"^\s*hey\b", normalized):
        en += 4
    if sv > en:
        return "sv"
    if en > sv:
        return "en"
    return _base_detect_language(text, previous)

_UNINSTALLED_FILE = DATA_DIR / "uninstalled_skills.json"

WAKE_RE = re.compile(
    r"^\s*(?:(?:hey|hej)[\s,]+)?(?:jarvis|järvis|jervis)\b[\s,.:;!?-]*",
    re.IGNORECASE,
)


def _read_uninstalled() -> set[str]:
    try:
        value = json.loads(_UNINSTALLED_FILE.read_text(encoding="utf-8"))
        return {str(item) for item in value if isinstance(item, str)}
    except Exception:
        return set()


def _write_uninstalled(values: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = _UNINSTALLED_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")
    temp.replace(_UNINSTALLED_FILE)


def _all_local_skill_manifests() -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(SKILLS_DIR.glob("*/skill.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and manifest.get("id"):
                found.append((manifest_path.parent, manifest))
        except Exception:
            continue
    return found


def _resolve_local_skill(value: str) -> tuple[Path, dict[str, Any]] | None:
    wanted = safe_id(value)
    lowered = value.strip().lower()
    for directory, manifest in _all_local_skill_manifests():
        skill_id = str(manifest.get("id", ""))
        name = str(manifest.get("name", ""))
        aliases = {
            skill_id.lower(),
            safe_id(skill_id),
            name.lower(),
            safe_id(name),
        }
        if lowered in aliases or wanted in aliases:
            return directory, manifest
    return None


def _clean_phantom_disabled() -> None:
    installed = {
        str(manifest.get("id"))
        for _, manifest in _all_local_skill_manifests()
        if manifest.get("id")
    }
    disabled = read_disabled()
    cleaned = disabled & installed
    if cleaned != disabled:
        write_disabled(cleaned)


class StableWakeModel:
    """Require a stable wake score before exposing a detection to Jarvis."""

    def __init__(self, inner: Any, settings: Any, logger: Any) -> None:
        self.inner = inner
        self.logger = logger
        self.models = inner.models
        self.debug = bool(getattr(settings, "debug", False))
        base_threshold = float(getattr(settings, "wake_threshold", 0.45))
        self.threshold = float(
            os.getenv("WAKE_CONFIRM_THRESHOLD", str(max(base_threshold, 0.50)))
        )
        self.strong_threshold = float(os.getenv("WAKE_STRONG_THRESHOLD", "0.88"))
        self.required_hits = max(1, int(os.getenv("WAKE_CONFIRM_HITS", "2")))
        self.window_frames = max(
            self.required_hits,
            int(os.getenv("WAKE_CONFIRM_WINDOW_FRAMES", "8")),
        )
        self.min_rms = float(os.getenv("WAKE_MIN_RMS", "120"))
        self._scores: deque[float] = deque(maxlen=self.window_frames)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def reset(self) -> None:
        self._scores.clear()
        self.inner.reset()

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        scores = dict(self.inner.predict(samples))
        raw = float(scores.get("hey_jarvis", 0.0))
        if samples.size:
            float_samples = samples.astype(np.float32, copy=False)
            rms = float(math.sqrt(float(np.mean(float_samples * float_samples))))
        else:
            rms = 0.0

        eligible = raw if rms >= self.min_rms else 0.0
        self._scores.append(eligible)
        hits = sum(score >= self.threshold for score in self._scores)
        accepted = (
            rms >= self.min_rms
            and (
                raw >= self.strong_threshold
                or (raw >= self.threshold and hits >= self.required_hits)
            )
        )

        if self.debug and raw >= 0.08:
            self.logger.debug(
                "Wake gate raw=%.3f rms=%.0f hits=%d/%d accepted=%s",
                raw,
                rms,
                hits,
                self.required_hits,
                accepted,
            )

        scores["hey_jarvis"] = raw if accepted else 0.0
        if accepted:
            self._scores.clear()
        return scores


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .skill_schema import SkillRegistry
    from .skills import SkillSystem
    from . import capability_flow

    original_jarvis_init = Jarvis.__init__
    original_handle_audio = Jarvis.handle_audio
    original_transcribe = Jarvis.transcribe
    original_say = Jarvis.say
    original_obvious_followup = Brain.obvious_followup
    original_instructions = Brain.instructions
    original_brain_ask = Brain.ask
    original_system_init = SkillSystem.__init__
    original_system_schemas = SkillSystem.schemas
    original_system_call = SkillSystem.call
    original_system_handles = SkillSystem.handles_tool
    original_prompt_context = SkillSystem.prompt_context
    original_manager_sync = SharedSkillManager.sync
    original_manager_list_state = SharedSkillManager.list_state

    Jarvis.wake_regex = WAKE_RE

    def patched_jarvis_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_jarvis_init(self, *args, **kwargs)
        self.wake_model = StableWakeModel(
            self.wake_model,
            self.settings,
            self.logger,
        )

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        prefetched = getattr(self, "_wake_confirmed_transcript", None)
        if prefetched is not None:
            self._wake_confirmed_transcript = None
            return str(prefetched)
        return original_transcribe(self, frames)

    def patched_say(
        self: Any,
        text: str,
        turn_started: float | None = None,
    ) -> None:
        if text == "Yes?" and getattr(self, "current_language", "en") == "sv":
            text = "Ja?"
        original_say(self, text, turn_started)

    def patched_handle_audio(
        self: Any,
        frames: list[bytes],
        interaction_started: float | None = None,
    ) -> None:
        transcript = original_transcribe(self, frames)
        if not WAKE_RE.match(transcript):
            self.logger.warning(
                "WAKE | rejected after transcript confirmation | transcript=%r",
                transcript,
            )
            if getattr(self.settings, "debug", False):
                print(f"WAKE REJECTED: {transcript!r}\n")
            return

        self.logger.info("WAKE | transcript confirmation passed")
        self._wake_confirmed_transcript = transcript
        original_handle_audio(self, frames, interaction_started)

    def patched_obvious_followup(self: Any, text: str) -> bool | None:
        result = original_obvious_followup(self, text)
        if result is not None:
            return result

        normalized = text.strip()
        lower = normalized.lower().strip(" .,!?:;")
        if not lower:
            return False

        if re.search(
            r"\b("
            r"you|your|yours|it|that|this|again|check|remove|delete|disable|"
            r"enable|skill|screen|screenshot|camera|webcam|"
            r"du|dig|din|ditt|den|det|igen|kolla|kontrollera|ta bort|"
            r"inaktivera|aktivera|skillen|skärmen|kameran"
            r")\b",
            lower,
            re.IGNORECASE,
        ):
            return True

        if normalized.endswith("?") or re.match(
            r"^(?:can|could|would|will|are|is|do|did|have|has|what|why|how|"
            r"kan|kunde|skulle|är|har|vad|varför|hur)\b",
            lower,
            re.IGNORECASE,
        ):
            return True

        return None

    def patched_brain_ask(self: Any, text: str) -> str:
        system = getattr(self.tools, "skill_system", None)
        manager = getattr(system, "shared_manager", None) if system is not None else None
        before = manager.list_state() if manager is not None else []
        answer = original_brain_ask(self, text)
        after = manager.list_state() if manager is not None else []

        before_ids = {str(item.get("id")) for item in before}
        after_ids = {str(item.get("id")) for item in after}
        answer_lower = answer.lower()
        language = detect_language(
            text, getattr(self, "response_language", "en")
        )

        installed_claim = bool(
            re.search(
                r"\b(i have|i've got|is installed|installed and ready|"
                r"jag har|finns installerad|är installerad)\b",
                answer_lower,
            )
            and re.search(r"\bskill", answer_lower)
        )
        known_names = [
            str(item.get("name", "")).lower()
            for item in after
            if item.get("name")
        ]
        names_support_claim = any(name in answer_lower for name in known_names)

        removal_claim = bool(
            re.search(
                r"\b(removed|deleted|uninstalled|took .* off|"
                r"tog bort|raderade|avinstallerade|borttagen)\b",
                answer_lower,
            )
            and re.search(r"\bskill", answer_lower)
        )

        replacement: str | None = None
        if installed_claim and (not after or not names_support_claim):
            replacement = (
                "Nej, den skillen är inte installerad."
                if language == "sv"
                else "No, that skill is not installed."
            )
        elif removal_claim and before_ids <= after_ids:
            before_state = {
                str(item.get("id")): str(item.get("state", ""))
                for item in before
            }
            after_state = {
                str(item.get("id")): str(item.get("state", ""))
                for item in after
            }
            disabled_instead = any(
                before_state.get(skill_id) == "enabled"
                and after_state.get(skill_id) == "disabled"
                for skill_id in before_ids & after_ids
            )
            if disabled_instead:
                replacement = (
                    "Skillen inaktiverades bara; den avinstallerades inte."
                    if language == "sv"
                    else "The skill was only disabled; it was not uninstalled."
                )
            else:
                replacement = (
                    "Den skillen var inte installerad, så inget togs bort."
                    if language == "sv"
                    else "That skill was not installed, so nothing was removed."
                )

        if replacement is not None:
            self.logger.error(
                "TRUTH GUARD | replaced unsupported skill-state claim | "
                "answer=%r | before=%s | after=%s",
                answer,
                sorted(before_ids),
                sorted(after_ids),
            )
            if self.history and self.history[-1].get("role") == "assistant":
                self.history[-1]["content"] = replacement
            return replacement
        return answer

    def patched_instructions(self: Any) -> str:
        base = original_instructions(self)
        return (
            f"{base}\n\n"
            "AUTHORITATIVE SKILL STATE\n"
            "- Never claim a skill exists, is installed, is enabled, was disabled, "
            "or was removed unless a skill-management tool confirms it.\n"
            "- list_installed_skills includes enabled and disabled installed skills. "
            "An empty list means no local skill is installed.\n"
            "- set_local_skill_enabled only enables or disables a skill. Disabling "
            "does not delete or uninstall it.\n"
            "- uninstall_local_skill is the only tool that removes local skill files.\n"
            "- If a skill tool returns found=false or changed=false, say the action "
            "did not happen. Never convert that failure into a success claim.\n"
            "- Do not invent names of installed skills from the user's description.\n"
        )

    def patched_system_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_system_init(self, *args, **kwargs)
        _clean_phantom_disabled()

    def patched_system_schemas(self: Any) -> list[dict[str, Any]]:
        schemas = original_system_schemas(self)
        for schema in schemas:
            if schema.get("name") == "set_local_skill_enabled":
                schema["description"] = (
                    "Enable or disable an actually installed Jarvis skill only on "
                    "this computer. This does not uninstall or delete the skill."
                )
                properties = schema.get("parameters", {}).get("properties", {})
                if "skill_id" in properties:
                    properties["skill_id"]["description"] = (
                        "The exact installed skill ID or name. Never invent one."
                    )

        if not any(
            schema.get("name") == "uninstall_local_skill" for schema in schemas
        ):
            schemas.append(
                {
                    "type": "function",
                    "name": "uninstall_local_skill",
                    "description": (
                        "Permanently remove an actually installed skill from this "
                        "computer. Shared synchronization will not reinstall it until "
                        "the user enables or installs it again."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {
                                "type": "string",
                                "description": "Exact installed skill ID or name.",
                            }
                        },
                        "required": ["skill_id"],
                        "additionalProperties": False,
                    },
                }
            )
        return schemas

    def patched_system_handles(self: Any, name: str) -> bool:
        return name == "uninstall_local_skill" or original_system_handles(self, name)

    def manager_set_enabled(
        self: Any,
        skill_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        match = _resolve_local_skill(skill_id)

        if match is None and enabled:
            normalized = safe_id(skill_id)
            uninstalled = _read_uninstalled()
            if normalized in uninstalled:
                uninstalled.discard(normalized)
                _write_uninstalled(uninstalled)
                original_manager_sync(self)
                match = _resolve_local_skill(skill_id)

        if match is None:
            return {
                "found": False,
                "changed": False,
                "requested_skill": skill_id,
                "error": "No installed skill matched that ID or name.",
            }

        _, manifest = match
        resolved_id = str(manifest["id"])
        disabled = read_disabled()
        before = resolved_id in disabled
        if enabled:
            disabled.discard(resolved_id)
        else:
            disabled.add(resolved_id)
        write_disabled(disabled)
        self.system.registry.reload()
        after = resolved_id in disabled
        return {
            "found": True,
            "changed": before != after,
            "skill_id": resolved_id,
            "skill_name": str(manifest.get("name", resolved_id)),
            "installed": True,
            "enabled": not after,
            "state": "enabled" if not after else "disabled",
            "uninstalled": False,
        }

    def manager_uninstall(self: Any, skill_id: str) -> dict[str, Any]:
        match = _resolve_local_skill(skill_id)
        if match is None:
            return {
                "found": False,
                "changed": False,
                "requested_skill": skill_id,
                "error": "No installed skill matched that ID or name.",
            }

        directory, manifest = match
        resolved_id = str(manifest["id"])
        name = str(manifest.get("name", resolved_id))
        shutil.rmtree(directory)

        disabled = read_disabled()
        disabled.discard(resolved_id)
        write_disabled(disabled)

        uninstalled = _read_uninstalled()
        uninstalled.add(resolved_id)
        _write_uninstalled(uninstalled)

        self.system.registry.reload()
        return {
            "found": True,
            "changed": True,
            "skill_id": resolved_id,
            "skill_name": name,
            "installed": False,
            "enabled": False,
            "state": "uninstalled",
            "uninstalled": True,
        }

    def patched_manager_sync(self: Any) -> dict[str, Any]:
        result = original_manager_sync(self)
        blocked = _read_uninstalled()
        removed: list[str] = []
        for skill_id in sorted(blocked):
            path = SKILLS_DIR / skill_id
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(skill_id)
        if removed:
            self.system.registry.reload()
        if isinstance(result, dict):
            result["locally_uninstalled"] = sorted(blocked)
            result["removed_after_sync"] = removed
        return result

    def patched_manager_list_state(self: Any) -> list[dict[str, Any]]:
        states = original_manager_list_state(self)
        for item in states:
            enabled = bool(item.get("enabled", True))
            item["installed"] = True
            item["state"] = "enabled" if enabled else "disabled"
        return states

    def patched_prompt_context(self: Any) -> str:
        base = original_prompt_context(self)
        manager = getattr(self, "shared_manager", None)
        inventory = manager.list_state() if manager is not None else []
        return (
            f"{base}\n\nAUTHORITATIVE INSTALLED SKILL INVENTORY:\n"
            f"{json.dumps(inventory, ensure_ascii=False)}"
        )

    def patched_system_call(
        self: Any,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        manager = getattr(self, "shared_manager", None)

        if name == "set_local_skill_enabled":
            if manager is None:
                raise RuntimeError("Shared skill manager is unavailable.")
            return manager.set_enabled(
                str(args["skill_id"]),
                bool(args["enabled"]),
            )

        if name == "uninstall_local_skill":
            if manager is None:
                raise RuntimeError("Shared skill manager is unavailable.")
            return manager.uninstall(str(args["skill_id"]))

        if name == "list_installed_skills" and manager is not None:
            return {"skills": manager.list_state()}

        return original_system_call(self, name, args)

    Jarvis.__init__ = patched_jarvis_init
    Jarvis.transcribe = patched_transcribe
    Jarvis.say = patched_say
    Jarvis.handle_audio = patched_handle_audio
    language_mode.detect_language = detect_language
    capability_flow.detect_language = detect_language
    Brain.obvious_followup = patched_obvious_followup
    Brain.instructions = patched_instructions
    Brain.ask = patched_brain_ask
    SkillSystem.__init__ = patched_system_init
    SkillSystem.schemas = patched_system_schemas
    SkillSystem.handles_tool = patched_system_handles
    SkillSystem.call = patched_system_call
    SkillSystem.prompt_context = patched_prompt_context
    SharedSkillManager.set_enabled = manager_set_enabled
    SharedSkillManager.uninstall = manager_uninstall
    SharedSkillManager.sync = patched_manager_sync
    SharedSkillManager.list_state = patched_manager_list_state
    _PATCHED = True
