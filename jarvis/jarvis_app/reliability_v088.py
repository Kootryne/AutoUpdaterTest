from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

import sounddevice as sd
import soundfile as sf

from . import capability_flow
from .language_mode import detect_language
from .paths import DATA_DIR

_PATCHED = False
_POWER_STATE = DATA_DIR / "pending_pc_power.json"
_TTS_CACHE_DIR = DATA_DIR / "local_tts" / "cache_v088"

_YES_RE = re.compile(
    r"^(?:yes(?: please| do it)?|yeah|yep|confirm|do it|go ahead|"
    r"ja|japp|ja tack|bekräfta|gör det|kör)[, ]*[.!?]*$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^(?:no|nope|cancel|stop|never mind|not now|"
    r"nej|nä|avbryt|stopp|inte nu|strunt samma)[.!?]*$",
    re.IGNORECASE,
)
_PC_TARGET_RE = re.compile(
    r"^(?:(?:(?:the|my|this)\s+)?(?:pc|computer|windows|machine)|"
    r"(?:(?:min|den här)\s+)?(?:pc|dator|datorn|windows))[.!?]*$",
    re.IGNORECASE,
)
_JARVIS_TARGET_RE = re.compile(
    r"^(?:jarvis|you|yourself|the assistant|dig|assistenten)$",
    re.IGNORECASE,
)
_PC_OFF_RE = re.compile(
    r"^(?:please\s+)?(?:shut\s*(?:down|off)|shutdown|turn\s+off|power\s+off)"
    r"\s+(?:(?:my|the|this)\s+)?(?:pc|computer|windows|machine)(?:\s+now)?[.!?]*$|"
    r"^(?:snälla\s+)?(?:stäng\s+av|slå\s+av)\s+"
    r"(?:(?:min|den här)\s+)?(?:dator|datorn|pc|pc:n|windows)(?:\s+nu)?[.!?]*$",
    re.IGNORECASE,
)
_PC_RESTART_RE = re.compile(
    r"^(?:please\s+)?(?:restart|reboot)\s+"
    r"(?:(?:my|the|this)\s+)?(?:pc|computer|windows|machine)(?:\s+now)?[.!?]*$|"
    r"^(?:snälla\s+)?starta\s+om\s+"
    r"(?:(?:min|den här)\s+)?(?:dator|datorn|pc|pc:n|windows)(?:\s+nu)?[.!?]*$",
    re.IGNORECASE,
)
_INCOMPLETE_SHUTDOWN_RE = re.compile(
    r"^(?:please\s+)?(?:shut\s*(?:down|off)|turn\s+off|power\s+off)"
    r"(?:\s+(?:the|my|this))?\s*(?:\.{2,})?[.!?]*$|"
    r"^(?:stäng\s+av|slå\s+av)(?:\s+(?:den|min|det))?\s*(?:\.{2,})?[.!?]*$",
    re.IGNORECASE,
)
_INCOMPLETE_RESTART_RE = re.compile(
    r"^(?:please\s+)?(?:restart|reboot)(?:\s+(?:the|my|this))?"
    r"\s*(?:\.{2,})?[.!?]*$|"
    r"^starta\s+om(?:\s+(?:den|min|det))?\s*(?:\.{2,})?[.!?]*$",
    re.IGNORECASE,
)
_FUZZY_SWEDISH_PC_RE = re.compile(
    r"^(?:det\s+är|de\s+e|det\s+e|stäng\s+av|stänga\s+av)\s+p(?:c)?[.!?]*$",
    re.IGNORECASE,
)
_POWER_HISTORY_RE = re.compile(
    r"(?:shut\s+down|turn\s+off|restart|reboot).{0,30}(?:pc|computer)|"
    r"(?:stänga?\s+av|starta\s+om).{0,30}(?:dator|pc)",
    re.IGNORECASE,
)
_TARGET_HISTORY_RE = re.compile(
    r"(?:pc|computer).{0,20}(?:or|eller).{0,20}jarvis|"
    r"jarvis.{0,20}(?:or|eller).{0,20}(?:pc|computer|dator)",
    re.IGNORECASE,
)


def _clear_power() -> None:
    _POWER_STATE.unlink(missing_ok=True)


def _read_power() -> dict[str, Any] | None:
    try:
        value = json.loads(_POWER_STATE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        expires = datetime.fromisoformat(str(value["expires_at"]))
        if datetime.now(timezone.utc) >= expires:
            _clear_power()
            return None
        value.setdefault("stage", "confirm")
        value.setdefault("confirmations", 0)
        return value
    except Exception:
        return None


def _write_power(
    action: str,
    *,
    stage: str,
    confirmations: int,
    language: str,
) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    value = {
        "action": action,
        "stage": stage,
        "confirmations": confirmations,
        "language": language,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=90)
        ).isoformat(),
    }
    temporary = _POWER_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(_POWER_STATE)


def _last_assistant_text(jarvis: Any) -> str:
    brain = getattr(jarvis, "brain", None)
    for item in reversed(getattr(brain, "history", []) or []):
        if item.get("role") == "assistant":
            return str(item.get("content") or "")
    return ""


def _action_from_text(text: str) -> str | None:
    lower = text.lower()
    if re.search(r"restart|reboot|starta\s+om", lower):
        return "restart"
    if re.search(r"shut|turn\s+off|power\s+off|stäng", lower):
        return "shutdown"
    return None


def _cancel_stale_capability(logger: Any) -> None:
    try:
        if capability_flow._read_pending() is not None:
            capability_flow._clear_pending()
            logger.info(
                "POWER | cleared stale capability confirmation before power flow"
            )
    except Exception:
        logger.exception("POWER | could not clear stale capability confirmation")


def _begin_confirmation(
    jarvis: Any,
    action: str,
    language: str,
    turn_started: float | None,
) -> None:
    _cancel_stale_capability(jarvis.logger)
    _write_power(
        action,
        stage="confirm",
        confirmations=0,
        language=language,
    )
    phrase = (
        "Bekräfta datoravstängning?"
        if language == "sv" and action == "shutdown"
        else "Bekräfta omstart av datorn?"
        if language == "sv"
        else "Confirm PC shutdown?"
        if action == "shutdown"
        else "Confirm PC restart?"
    )
    jarvis.say(phrase, turn_started)


def _execute_power(action: str) -> None:
    if sys.platform == "win32":
        command = [
            "shutdown.exe",
            "/s" if action == "shutdown" else "/r",
            "/t",
            "5",
            "/c",
            "Confirmed twice through Jarvis.",
        ]
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        command = ["shutdown", "-h" if action == "shutdown" else "-r", "now"]
        creationflags = 0
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _split_speech(text: str, maximum: int = 52) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    rough = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|(?<=[,;:])\s+", text)
        if part.strip()
    ]
    chunks: list[str] = []
    for part in rough:
        words = part.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > maximum:
                chunks.append(current)
                current = word
            else:
                current = candidate
        if current:
            chunks.append(current)
    return chunks or [text]


def _cache_path(manager: Any, text: str) -> Path:
    identity = (
        f"{manager.kokoro_voice}|{manager.kokoro_language}|"
        f"{manager.kokoro_speed}|{text}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _TTS_CACHE_DIR / f"{digest}.wav"


def _kokoro_chunk(manager: Any, text: str) -> tuple[Any, int, bool]:
    path = _cache_path(manager, text)
    if path.is_file():
        data, rate = sf.read(path, dtype="float32")
        return data, int(rate), True

    kokoro = manager._load_kokoro()
    samples, rate = kokoro.create(
        text,
        voice=manager.kokoro_voice,
        speed=manager.kokoro_speed,
        lang=manager.kokoro_language,
    )
    samples = samples * manager.volume
    _TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.wav")
    sf.write(temporary, samples, rate)
    temporary.replace(path)

    cached = sorted(
        _TTS_CACHE_DIR.glob("*.wav"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in cached[256:]:
        old.unlink(missing_ok=True)
    return samples, int(rate), False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain

    original_handler = Jarvis._handle_local_update_command
    original_say = Jarvis.say
    original_instructions = Brain.instructions
    original_capability_read = capability_flow._read_pending

    def patched_capability_read() -> dict[str, Any] | None:
        value = original_capability_read()
        if value is None:
            return None
        try:
            created = datetime.fromisoformat(str(value.get("created_at", "")))
            if datetime.now(timezone.utc) - created > timedelta(minutes=10):
                capability_flow._clear_pending()
                return None
        except Exception:
            capability_flow._clear_pending()
            return None
        return value

    def patched_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        language = detect_language(
            normalized,
            getattr(self, "current_language", "en"),
        )
        pending = _read_power()

        if pending is not None:
            action = str(pending.get("action") or "shutdown")
            stage = str(pending.get("stage") or "confirm")
            pending_language = str(pending.get("language") or language)

            if _NO_RE.match(normalized):
                _clear_power()
                self.say(
                    "Avbrutet." if pending_language == "sv" else "Cancelled.",
                    turn_started,
                )
                return True

            if stage == "target":
                if _PC_TARGET_RE.match(normalized):
                    _begin_confirmation(
                        self, action, pending_language, turn_started
                    )
                    return True
                if _JARVIS_TARGET_RE.match(normalized):
                    _clear_power()
                    canonical = (
                        "restart Jarvis"
                        if action == "restart"
                        else "shut Jarvis off"
                    )
                    return original_handler(self, canonical, turn_started)
                _clear_power()
                self.logger.info(
                    "POWER | target clarification cancelled by unrelated command"
                )

            elif stage == "intent":
                if _YES_RE.match(normalized) or _PC_TARGET_RE.match(normalized):
                    _begin_confirmation(
                        self, action, pending_language, turn_started
                    )
                    return True
                _clear_power()
                self.logger.info(
                    "POWER | fuzzy intent cancelled by unrelated command"
                )

            elif stage == "confirm":
                if _YES_RE.match(normalized):
                    confirmations = int(pending.get("confirmations", 0))
                    if confirmations < 1:
                        _write_power(
                            action,
                            stage="confirm",
                            confirmations=1,
                            language=pending_language,
                        )
                        self.say(
                            "Bekräfta en gång till."
                            if pending_language == "sv"
                            else "Confirm once more.",
                            turn_started,
                        )
                        return True

                    _clear_power()
                    self.say(
                        (
                            "Stänger av om fem sekunder."
                            if action == "shutdown"
                            else "Startar om om fem sekunder."
                        )
                        if pending_language == "sv"
                        else (
                            "Shutting down in five seconds."
                            if action == "shutdown"
                            else "Restarting in five seconds."
                        ),
                        turn_started,
                    )
                    _execute_power(action)
                    return True

                _clear_power()
                self.logger.info(
                    "POWER | confirmation cancelled by unrelated command"
                )

        last_assistant = _last_assistant_text(self)
        if _YES_RE.match(normalized) and _POWER_HISTORY_RE.search(last_assistant):
            action = _action_from_text(last_assistant) or "shutdown"
            history_language = detect_language(last_assistant, language)
            _cancel_stale_capability(self.logger)
            _write_power(
                action,
                stage="confirm",
                confirmations=1,
                language=history_language,
            )
            self.say(
                "Bekräfta en gång till."
                if history_language == "sv"
                else "Confirm once more.",
                turn_started,
            )
            return True

        if (
            _PC_TARGET_RE.match(normalized)
            and _TARGET_HISTORY_RE.search(last_assistant)
        ):
            action = _action_from_text(last_assistant) or "shutdown"
            history_language = detect_language(last_assistant, language)
            _begin_confirmation(
                self, action, history_language, turn_started
            )
            return True

        action = (
            "shutdown"
            if _PC_OFF_RE.match(normalized)
            else "restart"
            if _PC_RESTART_RE.match(normalized)
            else None
        )
        if action is not None:
            _begin_confirmation(self, action, language, turn_started)
            return True

        incomplete = (
            "shutdown"
            if _INCOMPLETE_SHUTDOWN_RE.match(normalized)
            else "restart"
            if _INCOMPLETE_RESTART_RE.match(normalized)
            else None
        )
        if incomplete is not None:
            _cancel_stale_capability(self.logger)
            _write_power(
                incomplete,
                stage="target",
                confirmations=0,
                language=language,
            )
            self.say(
                "Datorn eller Jarvis?"
                if language == "sv"
                else "The PC or Jarvis?",
                turn_started,
            )
            return True

        if _FUZZY_SWEDISH_PC_RE.match(normalized):
            _cancel_stale_capability(self.logger)
            _write_power(
                "shutdown",
                stage="intent",
                confirmations=0,
                language="sv",
            )
            self.say("Menade du stäng av datorn?", turn_started)
            return True

        return original_handler(self, command, turn_started)

    def patched_say(
        self: Any,
        text: str,
        turn_started: float | None = None,
    ) -> None:
        manager = getattr(self, "local_tts_manager", None)
        if (
            manager is None
            or manager.backend() != "local"
            or not self.settings.tts_enabled
        ):
            original_say(self, text, turn_started)
            return

        language = detect_language(
            text,
            getattr(self, "current_language", "en"),
        )
        if language == "sv":
            original_say(self, text, turn_started)
            return

        speech = self.clean_speech(text)[:1800]
        chunks = _split_speech(speech)
        if not chunks:
            return

        started = time.perf_counter()
        self.audio.speaking.set()
        self.audio.disable()
        print(f"JARVIS: {text}\n")
        self.logger.info("Jarvis: %s", text)

        try:
            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="JarvisKokoro",
            ) as executor:
                chunk_started = time.perf_counter()
                current_data, current_rate, cached = _kokoro_chunk(
                    manager, chunks[0]
                )
                self.logger.info(
                    "LOCAL TTS | first chunk %.3fs | cached=%s | chars=%d",
                    time.perf_counter() - chunk_started,
                    cached,
                    len(chunks[0]),
                )

                for index, _ in enumerate(chunks):
                    future = (
                        executor.submit(
                            _kokoro_chunk,
                            manager,
                            chunks[index + 1],
                        )
                        if index + 1 < len(chunks)
                        else None
                    )
                    if index == 0 and turn_started is not None:
                        self.logger.info(
                            "TIMING | turn until local playback starts: %.3fs",
                            time.perf_counter() - turn_started,
                        )
                    playback_started = time.perf_counter()
                    sd.play(
                        current_data,
                        current_rate,
                        device=self.settings.speaker_device,
                        blocking=True,
                    )
                    self.logger.info(
                        "TIMING | local TTS chunk playback: %.3fs | "
                        "chunk=%d/%d | audio=%.2fs",
                        time.perf_counter() - playback_started,
                        index + 1,
                        len(chunks),
                        len(current_data) / current_rate
                        if current_rate
                        else 0.0,
                    )
                    if future is not None:
                        current_data, current_rate, _ = future.result()
        except Exception:
            self.logger.exception("LOCAL TTS | cached streaming playback failed")
            try:
                manager.set_backend("cloud")
            except Exception:
                pass
            self.audio.speaking.clear()
            self.audio.flush()
            original_say(self, text, turn_started)
            return
        finally:
            self.logger.info(
                "TIMING | local TTS streamed total: %.3fs",
                time.perf_counter() - started,
            )
            time.sleep(0.12)
            self.audio.speaking.clear()
            self.audio.flush()

    def patched_instructions(self: Any) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "PC POWER SAFETY\n"
            "- Never claim that a PC shutdown or restart is pending.\n"
            "- PC power actions and confirmations are handled locally, not by tools.\n"
            "- If a power request is unclear, ask the user to repeat the complete "
            "phrase 'shut down my PC' or 'restart my PC'.\n"
            "- Never interpret a bare yes as GitHub approval after discussing PC power.\n"
        )

    capability_flow._read_pending = patched_capability_read
    Jarvis._handle_local_update_command = patched_handler
    Jarvis.say = patched_say
    Brain.instructions = patched_instructions
    _PATCHED = True
