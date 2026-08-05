from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

import numpy as np
import sounddevice as sd
import soundfile as sf

from . import capability_flow
from .language_mode import detect_language
from .local_tts import LocalTTSManager
from .paths import ENV_FILE

_PATCHED = False

_LOCAL_TTS_RE = re.compile(
    r"(?:"
    r"\b(?:switch|change|use|enable|turn on)\b.{0,35}\b(?:local|offline)\b.{0,15}\b(?:tts|voice|speech)|"
    r"\b(?:local|offline)\s+(?:tts|voice|speech)\b|"
    r"\b(?:byt|växla|använd|aktivera|slå på)\b.{0,35}\b(?:lokal|offline)\b.{0,15}\b(?:tts|röst|talsyntes)|"
    r"\b(?:lokal|offline)\s+(?:tts|röst|talsyntes)\b"
    r")",
    re.IGNORECASE,
)
_CLOUD_TTS_RE = re.compile(
    r"(?:"
    r"\b(?:switch|change|use|enable|turn on)\b.{0,35}\b(?:cloud|openai|online)\b.{0,15}\b(?:tts|voice|speech)|"
    r"\b(?:cloud|openai|online)\s+(?:tts|voice|speech)\b|"
    r"\b(?:byt|växla|använd|aktivera|slå på)\b.{0,35}\b(?:moln|openai|online)\b.{0,15}\b(?:tts|röst|talsyntes)|"
    r"\b(?:moln|openai|online)\s+(?:tts|röst|talsyntes)\b"
    r")",
    re.IGNORECASE,
)
_TTS_STATUS_RE = re.compile(
    r"(?:"
    r"\bwhat\s+(?:tts|voice|speech)\b.{0,25}\b(?:using|active)|"
    r"\bwhich\s+(?:tts|voice)\b|"
    r"\b(?:tts|voice)\s+status\b|"
    r"\bvilken\s+(?:tts|röst|talsyntes)\b|"
    r"\b(?:tts|röst|talsyntes)\s*(?:status|använder du)\b"
    r")",
    re.IGNORECASE,
)

_VISION_RE = re.compile(
    r"\b(?:camera|webcam|screenshot|screen\s*capture|look\s+at\s+(?:my\s+)?screen|"
    r"kamera|webbkamera|skärmbild|skärmdump|skärminspelning|titta\s+på\s+skärmen)\b",
    re.IGNORECASE,
)
_SKILL_WORD_RE = re.compile(r"\b(?:skill|skills|skillen|skillar)\b", re.IGNORECASE)
_SKILL_EXISTS_RE = re.compile(
    r"(?:"
    r"\b(?:is|does)\s+(?:it|that|the\s+skill)\b.{0,20}\b(?:exist|installed|built|ready)|"
    r"\bdo\s+you\s+(?:have|already\s+have)\b.{0,25}\bskill\b|"
    r"\bfinns\s+(?:det|den)\b.{0,25}\b(?:redan|installerad|byggd|klar)|"
    r"\bhar\s+du\b.{0,25}\bskill\b|"
    r"\bär\s+den\b.{0,20}\b(?:installerad|byggd|klar)\b"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_BUILD_RE = re.compile(
    r"^(?:"
    r"(?:please\s+)?(?:make|build|create|program)\s+(?:it|that|the\s+skill)(?:\s+now)?|"
    r"(?:you\s+(?:have|need)\s+to\s+(?:make|build|create)\s+(?:it|that)(?:\s+now)?)|"
    r"(?:gör|bygg|skapa|programmera)\s+(?:den|det|skillen)(?:\s+nu)?|"
    r"(?:men\s+)?gör\s+den\s+då|"
    r"du\s+måste\s+göra\s+(?:den|det)(?:\s+nu)?"
    r")[.!?]*$",
    re.IGNORECASE,
)

_MODEL_MIGRATIONS = {
    "TEXT_MODEL": {"gpt-4.1-mini": "gpt-5.6-luna"},
    "FOLLOWUP_MODEL": {"gpt-4.1-nano": "gpt-5.4-nano"},
    "SKILL_PLANNER_MODEL": {"gpt-5.2": "gpt-5.6-sol"},
    "SKILL_BUILDER_MODEL": {"gpt-5.1": "gpt-5.6-luna"},
    "SKILL_RUNTIME_MODEL": {"gpt-5.1": "gpt-5.6-luna"},
}


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _migrate_env_and_settings(settings: Any, logger: Any) -> None:
    attributes = {
        "TEXT_MODEL": "text_model",
        "FOLLOWUP_MODEL": "followup_model",
        "SKILL_PLANNER_MODEL": "skill_planner_model",
        "SKILL_BUILDER_MODEL": "skill_builder_model",
        "SKILL_RUNTIME_MODEL": "skill_runtime_model",
    }
    changed: dict[str, tuple[str, str]] = {}
    for env_name, migrations in _MODEL_MIGRATIONS.items():
        attribute = attributes[env_name]
        current = str(getattr(settings, attribute, "")).strip()
        replacement = migrations.get(current)
        if replacement:
            setattr(settings, attribute, replacement)
            changed[env_name] = (current, replacement)

    if not changed:
        return

    try:
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        output: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            match = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
            if not match:
                output.append(line)
                continue
            name, value = match.group(1), match.group(2)
            seen.add(name)
            migration = changed.get(name)
            output.append(
                f"{name}={migration[1]}"
                if migration and value == migration[0]
                else line
            )
        for name, (_, replacement) in changed.items():
            if name not in seen:
                output.append(f"{name}={replacement}")
        ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    except Exception:
        logger.exception("MODELS | could not persist model migration")

    logger.info(
        "MODELS | migrated defaults: %s",
        ", ".join(f"{key}={old}->{new}" for key, (old, new) in changed.items()),
    )


def _inventory(skill_system: Any) -> list[dict[str, Any]]:
    manager = getattr(skill_system, "shared_manager", None)
    if manager is not None:
        return list(manager.list_state())
    return [
        {
            "id": definition.id,
            "name": definition.name,
            "enabled": True,
            "installed": True,
            "state": "enabled",
        }
        for definition in skill_system.registry.skills.values()
    ]


def _vision_skill(inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in inventory:
        haystack = " ".join(
            str(item.get(key, "")) for key in ("id", "name", "description")
        )
        if _VISION_RE.search(haystack):
            return item
    return None


def _replace_last_assistant(brain: Any, text: str) -> None:
    if brain.history and brain.history[-1].get("role") == "assistant":
        brain.history[-1]["content"] = text


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .skills import SkillSystem

    original_init = Jarvis.__init__
    original_say = Jarvis.say
    original_followup = Jarvis.record_followup
    original_skill_handler = Jarvis._handle_local_skill_command
    original_ask = Brain.ask
    original_response = Brain.create_response
    original_instructions = Brain.instructions
    original_start_build = SkillSystem.start_build

    def patched_init(
        self: Any,
        settings: Any,
        config: dict[str, Any],
        logger: Any,
    ) -> None:
        _migrate_env_and_settings(settings, logger)
        original_init(self, settings, config, logger)
        self.local_tts_manager = LocalTTSManager(logger)

    def patched_say(
        self: Any,
        text: str,
        turn_started: float | None = None,
    ) -> None:
        manager = getattr(self, "local_tts_manager", None)
        if manager is None or manager.backend() != "local" or not self.settings.tts_enabled:
            original_say(self, text, turn_started)
            return

        speech = self.clean_speech(text)[:1800]
        if not speech:
            return
        language = detect_language(text, getattr(self, "current_language", "en"))
        started = time.perf_counter()
        path = self.temp_path(".wav")
        self.audio.speaking.set()
        self.audio.disable()
        try:
            synth_started = time.perf_counter()
            manager.synthesize_to_wav(speech, language, path)
            self.logger.info(
                "LOCAL TTS | synthesis %.3fs | voice=%s | chars=%d",
                time.perf_counter() - synth_started,
                manager.voice_name(language),
                len(speech),
            )
            print(f"JARVIS: {text}\n")
            self.logger.info("Jarvis: %s", text)
            data, rate = sf.read(path, dtype="float32")
            if turn_started is not None:
                self.logger.info(
                    "TIMING | turn until local playback starts: %.3fs",
                    time.perf_counter() - turn_started,
                )
            playback_started = time.perf_counter()
            sd.play(data, rate, device=self.settings.speaker_device, blocking=True)
            self.logger.info(
                "TIMING | local TTS playback: %.3fs | audio=%.2fs",
                time.perf_counter() - playback_started,
                len(data) / rate if rate else 0.0,
            )
        except Exception:
            self.logger.exception("LOCAL TTS | synthesis or playback failed")
            try:
                manager.set_backend("cloud")
            except Exception:
                pass
            self.audio.speaking.clear()
            self.audio.flush()
            path.unlink(missing_ok=True)
            original_say(self, text, turn_started)
            return
        finally:
            self.logger.info(
                "TIMING | local TTS total: %.3fs",
                time.perf_counter() - started,
            )
            path.unlink(missing_ok=True)
            time.sleep(0.12)
            self.audio.speaking.clear()
            self.audio.flush()

    def patched_followup(self: Any) -> list[bytes] | None:
        frames = original_followup(self)
        if not frames:
            return frames
        maximum = 0.0
        for frame in frames:
            samples = np.frombuffer(frame, dtype=np.int16)
            if samples.size:
                values = samples.astype(np.float32, copy=False)
                maximum = max(maximum, math.sqrt(float(np.mean(values * values))))
        minimum = float(os.getenv("FOLLOWUP_MIN_CAPTURE_RMS", "250"))
        if maximum < minimum:
            self.logger.info(
                "FOLLOW-UP | rejected low-energy capture | max_rms=%.0f minimum=%.0f",
                maximum,
                minimum,
            )
            return None
        return frames

    def patched_skill_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        language = detect_language(normalized, getattr(self, "current_language", "en"))
        tts = getattr(self, "local_tts_manager", None)

        if tts is not None and _LOCAL_TTS_RE.search(normalized):
            if tts.backend() == "local":
                self.say(
                    "Lokal talsyntes är redan aktiv."
                    if language == "sv"
                    else "Local speech is already active.",
                    turn_started,
                )
                return True
            original_say(
                self,
                "Jag laddar ner de lokala rösterna. Det händer bara första gången."
                if language == "sv"
                else "I'm downloading the local voices. This only happens once.",
                turn_started,
            )
            try:
                tts.ensure_ready()
                tts.set_backend("local")
            except Exception:
                self.logger.exception("LOCAL TTS | setup failed")
                self.say(
                    "Jag kunde inte installera den lokala rösten."
                    if language == "sv"
                    else "I couldn't install the local voice.",
                    None,
                )
                return True
            self.say(
                "Lokal talsyntes är nu aktiv."
                if language == "sv"
                else "Local speech is now active.",
                None,
            )
            return True

        if tts is not None and _CLOUD_TTS_RE.search(normalized):
            tts.set_backend("cloud")
            self.say(
                "OpenAI-rösten är nu aktiv."
                if language == "sv"
                else "The OpenAI voice is now active.",
                turn_started,
            )
            return True

        if tts is not None and _TTS_STATUS_RE.search(normalized):
            local = tts.backend() == "local"
            self.say(
                (
                    "Jag använder lokal Piper-talsyntes."
                    if local
                    else "Jag använder OpenAI-talsyntes."
                )
                if language == "sv"
                else (
                    "I'm using local Piper speech."
                    if local
                    else "I'm using OpenAI speech."
                ),
                turn_started,
            )
            return True

        inventory = _inventory(self.skill_system)
        installed_vision = _vision_skill(inventory)
        if _VISION_RE.search(normalized) and _SKILL_WORD_RE.search(normalized):
            if installed_vision is not None:
                state = str(installed_vision.get("state", "enabled"))
                self.say(
                    f"{installed_vision.get('name', 'Skillen')} är installerad och {state}."
                    if language == "sv"
                    else f"{installed_vision.get('name', 'The skill')} is installed and {state}.",
                    turn_started,
                )
                return True
            capability_flow._save_pending(
                {
                    "kind": "core",
                    "goal": normalized,
                    "name": (
                        "Kamera- och skärmvisning"
                        if language == "sv"
                        else "Camera and screen viewing"
                    ),
                    "how_it_would_work": (
                        "Add permission-controlled screenshot and webcam capture tools "
                        "to Jarvis core, with explicit user requests before every capture."
                    ),
                    "reason": (
                        "Generated skills are sandboxed and cannot access cameras, "
                        "screens, devices, or unrestricted Windows APIs."
                    ),
                    "language": language,
                }
            )
            self.say(
                "Det kräver en kärnfunktion, inte en vanlig skill. Ska jag lägga upp förslaget?"
                if language == "sv"
                else "That needs a core feature, not a normal skill. Should I post it?",
                turn_started,
            )
            return True

        pending_capability = capability_flow._read_pending()
        pending_skill = self.skill_system.pending()
        if _SKILL_EXISTS_RE.search(normalized) and (
            _SKILL_WORD_RE.search(normalized)
            or pending_capability is not None
            or pending_skill is not None
        ):
            if inventory:
                names = ", ".join(
                    str(item.get("name") or item.get("id")) for item in inventory
                )
                self.say(
                    f"Installerade skills: {names}."
                    if language == "sv"
                    else f"Installed skills: {names}.",
                    turn_started,
                )
            else:
                self.say(
                    "Nej, den är inte installerad ännu."
                    if language == "sv"
                    else "No, it isn't installed yet.",
                    turn_started,
                )
            return True

        if _EXPLICIT_BUILD_RE.match(normalized):
            if pending_capability is not None and str(pending_capability.get("kind")) == "core":
                self.say(
                    "Den kan inte byggas som en säker skill. Ska jag lägga upp kärnförslaget?"
                    if language == "sv"
                    else "It can't be built as a safe skill. Should I post the core proposal?",
                    turn_started,
                )
                return True
            if pending_skill is not None:
                result = self.skill_system.start_pending_build()
                if result.get("already_running"):
                    answer = "Den byggs redan." if language == "sv" else "It's already being built."
                elif result.get("started"):
                    answer = "Jag bygger den i bakgrunden." if language == "sv" else "I'm building it in the background."
                else:
                    answer = "Jag kunde inte starta bygget." if language == "sv" else "I couldn't start the build."
                self.say(answer, turn_started)
                return True

        return original_skill_handler(self, command, turn_started)

    def patched_start_build(
        self: Any,
        *,
        requested_capability: Any,
        suggested_name: Any,
        how_it_would_work: Any,
    ) -> dict[str, Any]:
        proposal = self.pending() or {}
        goal = str(requested_capability or proposal.get("requested_capability") or "").strip()
        name = str(suggested_name or proposal.get("suggested_name") or "New skill").strip()
        goal_key = _safe_id(goal)[:80]
        name_key = _safe_id(name)
        for record in self.tasks.active():
            if record.kind != "skill_build":
                continue
            metadata = record.metadata or {}
            existing_goal = _safe_id(str(metadata.get("goal", "")))[:80]
            existing_name = _safe_id(str(metadata.get("suggested_name", "")))
            if (goal_key and goal_key == existing_goal) or (name_key and name_key == existing_name):
                self.clear_pending()
                return {
                    "started": False,
                    "already_running": True,
                    "task_id": record.id,
                    "title": record.title,
                    "message": "An equivalent skill build is already running.",
                }
        return original_start_build(
            self,
            requested_capability=requested_capability,
            suggested_name=suggested_name,
            how_it_would_work=how_it_would_work,
        )

    def patched_ask(self: Any, text: str) -> str:
        system = getattr(self.tools, "skill_system", None)
        before = {
            record.id
            for record in system.tasks.active()
            if record.kind == "skill_build"
        } if system is not None else set()
        answer = original_ask(self, text)
        records = [
            record for record in system.tasks.active() if record.kind == "skill_build"
        ] if system is not None else []
        after = {record.id for record in records}
        language = detect_language(text, getattr(self, "response_language", "en"))
        replacement: str | None = None
        if after - before:
            replacement = "Jag bygger den i bakgrunden." if language == "sv" else "I'm building it in the background."
        elif _EXPLICIT_BUILD_RE.match(text.strip()) and records:
            replacement = "Den byggs redan." if language == "sv" else "It's already being built."
        elif _SKILL_EXISTS_RE.search(text) and system is not None and not _inventory(system):
            replacement = "Nej, den är inte installerad ännu." if language == "sv" else "No, it isn't installed yet."
        if replacement is not None and replacement != answer:
            self.logger.info("SKILLS | corrected response from authoritative task/inventory state")
            _replace_last_assistant(self, replacement)
            return replacement
        return answer

    def patched_response(self: Any, label: str, **kwargs: Any) -> Any:
        model = str(kwargs.get("model", ""))
        if model.startswith("gpt-5") and "reasoning" not in kwargs:
            kwargs["reasoning"] = {
                "effort": "none" if "nano" in model and "follow-up" in label else "low"
            }
        return original_response(self, label, **kwargs)

    def patched_instructions(self: Any) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "MODEL ROLES\n"
            "- Normal conversation uses GPT-5.6 Luna.\n"
            "- Sol means gpt-5.6-sol and handles high-reasoning skill planning.\n"
            "- Luna means gpt-5.6-luna and handles implementation and runtime.\n\n"
            "BACKGROUND BUILD TRUTH\n"
            "- A started background build is not an installed skill yet.\n"
            "- If build_new_skill returns started=true, say only that the build started.\n"
            "- If it returns already_running=true, say it is already being built.\n"
            "- Never call build_new_skill twice for the same active capability.\n"
            "- Camera, webcam, screenshot, and screen capture require Jarvis core permissions.\n"
        )

    Jarvis.__init__ = patched_init
    Jarvis.say = patched_say
    Jarvis.record_followup = patched_followup
    Jarvis._handle_local_skill_command = patched_skill_handler
    SkillSystem.start_build = patched_start_build
    Brain.ask = patched_ask
    Brain.create_response = patched_response
    Brain.instructions = patched_instructions
    _PATCHED = True
