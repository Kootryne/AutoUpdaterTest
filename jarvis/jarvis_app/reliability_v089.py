from __future__ import annotations

from io import BytesIO
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any
import wave

import numpy as np

from .language_mode import detect_language
from .paths import FRAME_MS, SAMPLE_RATE
from .session_logging import SessionLogManager

_PATCHED = False
_SILENT_ACK = "\x00JARVIS_SILENT_ACK\x00"

_ACK_RE = re.compile(
    r"^(?:ok(?:ay)?|okay then|alright|got it|understood|makes sense|"
    r"thanks|thank you|okej|ok då|fattar|förstår|tack)[.!?]*$",
    re.IGNORECASE,
)
_NATURAL_ANSWER_RE = re.compile(
    r"^(?:i (?:forgot|don't know|dont know|can't remember|cant remember)|"
    r"not sure|no idea|jag (?:glömde|vet inte|minns inte)|ingen aning)[.!?]*$",
    re.IGNORECASE,
)
_SESSION_STATUS_RE = re.compile(
    r"\b(?:current session(?: log)?|session log status|session id|"
    r"aktuell sessionslogg|sessions-id|vilken session)\b",
    re.IGNORECASE,
)
_SESSION_OPEN_RE = re.compile(
    r"\b(?:open|show|view)\b.{0,25}\b(?:session logs?|conversation logs?)\b|"
    r"\b(?:öppna|visa)\b.{0,25}\b(?:sessionsloggar?|samtalsloggar?)\b",
    re.IGNORECASE,
)
_SESSION_EXPORT_RE = re.compile(
    r"\b(?:export|save|zip)\b.{0,25}\b(?:current )?(?:session|conversation) logs?\b|"
    r"\b(?:exportera|spara|zippa)\b.{0,25}\b(?:sessionslogg|samtalslogg)\b",
    re.IGNORECASE,
)
_SESSION_LIST_RE = re.compile(
    r"\b(?:list|recent)\b.{0,20}\b(?:session|conversation) logs?\b|"
    r"\b(?:lista|senaste)\b.{0,20}\b(?:sessionsloggar?|samtalsloggar?)\b",
    re.IGNORECASE,
)


def _session_manager(value: Any) -> SessionLogManager | None:
    logger = value if isinstance(value, logging.Logger) else getattr(value, "logger", None)
    manager = getattr(logger, "session_logs", None)
    return manager if isinstance(manager, SessionLogManager) else None


def _memory_wav(frames: list[bytes]) -> BytesIO:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(b"".join(frames))
    buffer.seek(0)
    buffer.name = "jarvis_speech.wav"  # type: ignore[attr-defined]
    return buffer


def _fast_split(text: str, maximum: int = 22) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|(?<=[,;:])\s+", text)
        if part.strip()
    ]
    chunks: list[str] = []
    for part in parts:
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


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .tools import Tools
    from . import reliability_v088

    original_run = Jarvis.run
    original_handle_audio = Jarvis.handle_audio
    original_say = Jarvis.say
    original_followup = Jarvis.record_followup
    original_update_handler = Jarvis._handle_local_update_command
    original_ask = Brain.ask
    original_obvious_followup = Brain.obvious_followup
    original_instructions = Brain.instructions
    original_schemas = Tools.schemas
    original_call = Tools.call

    # The v088 closure resolves this module global dynamically.
    reliability_v088._split_speech = _fast_split

    def patched_run(self: Any) -> None:
        manager = _session_manager(self)
        tts = getattr(self, "local_tts_manager", None)

        if tts is not None and tts.backend() == "local":
            prewarm_started = time.perf_counter()
            self.logger.info("LOCAL TTS | prewarming before microphone startup")
            try:
                reliability_v088._kokoro_chunk(tts, "Ready.")
                self.logger.info(
                    "LOCAL TTS | prewarm complete: %.3fs",
                    time.perf_counter() - prewarm_started,
                )
            except Exception:
                self.logger.exception("LOCAL TTS | prewarm failed; continuing")

        def warm_api_connection() -> None:
            try:
                started = time.perf_counter()
                self.client.models.retrieve(self.settings.stt_model)
                self.logger.info(
                    "STT | API connection warmup: %.3fs",
                    time.perf_counter() - started,
                )
            except Exception:
                self.logger.debug("STT | API connection warmup failed", exc_info=True)

        threading.Thread(
            target=warm_api_connection,
            name="JarvisAPIWarmup",
            daemon=True,
        ).start()

        try:
            original_run(self)
        finally:
            if manager is not None:
                manager.close(
                    "update" if getattr(self, "process_exit_code", 0) == 42
                    else "stopped"
                )

    def patched_handle_audio(
        self: Any,
        frames: list[bytes],
        interaction_started: float | None = None,
    ) -> None:
        manager = _session_manager(self)
        conversation_id = (
            manager.start_conversation("voice") if manager is not None else None
        )
        self.logger.info(
            "SESSION | conversation started | run=%s conversation=%s",
            manager.run_id if manager else "disabled",
            conversation_id or "disabled",
        )
        status = "completed"
        try:
            return original_handle_audio(self, frames, interaction_started)
        except Exception:
            status = "failed"
            raise
        finally:
            if manager is not None:
                manager.end_conversation(conversation_id, status=status)
            self.logger.info(
                "SESSION | conversation ended | conversation=%s status=%s",
                conversation_id or "disabled",
                status,
            )

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        if not frames:
            return ""

        total_started = time.perf_counter()
        audio_seconds = len(frames) * FRAME_MS / 1000
        pre_roll_count = max(
            1, int(float(self.settings.pre_roll) * 1000 / FRAME_MS)
        )
        command_frames = frames[min(pre_roll_count, len(frames)) :]
        if command_frames:
            measurements = [self.detector.measure(frame) for frame in command_frames]
            max_rms = max((measurement.rms for measurement in measurements), default=0.0)
            vad_ratio = (
                sum(int(measurement.vad) for measurement in measurements)
                / len(measurements)
            )
            skip_max_rms = float(os.getenv("STT_WAKE_ONLY_MAX_RMS", "120"))
            skip_max_vad = float(os.getenv("STT_WAKE_ONLY_MAX_VAD_RATIO", "0.08"))
            if max_rms < skip_max_rms and vad_ratio <= skip_max_vad:
                transcript = (
                    "Hej Jarvis"
                    if getattr(self, "current_language", "en") == "sv"
                    else "Hey Jarvis"
                )
                self.logger.info(
                    "STT | skipped wake-only cloud transcription | "
                    "max_rms=%.0f vad_ratio=%.1f%% synthetic=%r",
                    max_rms,
                    vad_ratio * 100,
                    transcript,
                )
                manager = _session_manager(self)
                if manager is not None:
                    manager.record_transcript(
                        "user",
                        transcript,
                        source="wake-only",
                        language=getattr(self, "current_language", None),
                    )
                return transcript

        encode_started = time.perf_counter()
        audio_file = _memory_wav(frames)
        self.logger.info(
            "TIMING | WAV encode memory: %.3fs | audio=%.2fs | bytes=%d",
            time.perf_counter() - encode_started,
            audio_seconds,
            audio_file.getbuffer().nbytes,
        )

        api_started = time.perf_counter()
        result = self.client.audio.transcriptions.create(
            model=self.settings.stt_model,
            file=audio_file,
            response_format="json",
            prompt=(
                "The speaker may use Swedish, English, or both. "
                "Expected words include Jarvis, Viktor, Home Assistant, "
                "Stockholm, Göteborg, Gothenburg, Spotify, Steam, GitHub, "
                "session logs, shutdown, restart, and smart-home commands."
            ),
        )
        api_elapsed = time.perf_counter() - api_started
        transcript = str(result.text).strip()
        self.logger.info(
            "TIMING | STT API: %.3fs | audio=%.2fs | "
            "realtime_factor=%.2fx | chars=%d",
            api_elapsed,
            audio_seconds,
            api_elapsed / audio_seconds if audio_seconds > 0 else 0.0,
            len(transcript),
        )
        self.logger.info(
            "TIMING | STT total: %.3fs",
            time.perf_counter() - total_started,
        )
        manager = _session_manager(self)
        if manager is not None and transcript:
            manager.record_transcript(
                "user",
                transcript,
                source="stt",
                language=getattr(self, "current_language", None),
            )
        return transcript

    def patched_ask(self: Any, text: str) -> str:
        normalized = text.strip()
        previous = ""
        for item in reversed(self.history):
            if item.get("role") == "assistant":
                previous = str(item.get("content") or "").strip()
                break
        if _ACK_RE.match(normalized) and not previous.endswith("?"):
            self.logger.info(
                "CONVERSATION | acknowledgment accepted silently | transcript=%r",
                normalized,
            )
            return _SILENT_ACK
        return original_ask(self, text)

    def patched_say(
        self: Any,
        text: str,
        turn_started: float | None = None,
    ) -> None:
        if text == _SILENT_ACK:
            self._v089_silent_ack = True
            return

        manager = _session_manager(self)
        if manager is not None and text:
            manager.record_transcript(
                "assistant",
                text,
                source="jarvis",
                language=detect_language(
                    text, getattr(self, "current_language", "en")
                ),
            )
        return original_say(self, text, turn_started)

    def patched_followup(self: Any) -> Any:
        if bool(getattr(self, "_v089_silent_ack", False)):
            self._v089_silent_ack = False
            self.logger.info(
                "CONVERSATION | ended after silent acknowledgment"
            )
            return None
        return original_followup(self)

    def patched_obvious_followup(self: Any, text: str) -> bool | None:
        normalized = text.strip()
        if _NATURAL_ANSWER_RE.match(normalized):
            self.logger.info(
                "FOLLOW-UP | accepted direct answer locally | transcript=%r",
                normalized,
            )
            return True

        previous = ""
        for item in reversed(self.history):
            if item.get("role") == "assistant":
                previous = str(item.get("content") or "").strip()
                break
        words = re.findall(r"\b[\wåäöÅÄÖ']+\b", normalized)
        if previous.endswith("?") and 1 <= len(words) <= 14:
            self.logger.info(
                "FOLLOW-UP | accepted short answer to direct question | "
                "words=%d transcript=%r",
                len(words),
                normalized,
            )
            return True

        return original_obvious_followup(self, text)

    def patched_update_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        manager = _session_manager(self)
        language = detect_language(
            normalized, getattr(self, "current_language", "en")
        )

        if manager is not None and _SESSION_EXPORT_RE.search(normalized):
            try:
                destination = manager.export_current()
                self.say(
                    "Sessionsloggen exporterades till skrivbordet."
                    if language == "sv"
                    else "The session log was exported to your desktop.",
                    turn_started,
                )
                self.logger.info("SESSION | exported current run | path=%s", destination)
            except Exception:
                self.logger.exception("SESSION | export failed")
                self.say(
                    "Jag kunde inte exportera sessionsloggen."
                    if language == "sv"
                    else "I couldn't export the session log.",
                    turn_started,
                )
            return True

        if manager is not None and _SESSION_OPEN_RE.search(normalized):
            try:
                _open_path(manager.root)
                self.say(
                    "Öppnar sessionsloggarna."
                    if language == "sv"
                    else "Opening the session logs.",
                    turn_started,
                )
            except Exception:
                self.logger.exception("SESSION | open folder failed")
                self.say(
                    "Jag kunde inte öppna loggmappen."
                    if language == "sv"
                    else "I couldn't open the log folder.",
                    turn_started,
                )
            return True

        if manager is not None and _SESSION_LIST_RE.search(normalized):
            sessions = manager.recent_sessions(5)
            self.say(
                (
                    f"Det finns {len(sessions)} nyliga sessionsloggar."
                    if language == "sv"
                    else f"There are {len(sessions)} recent session logs."
                ),
                turn_started,
            )
            return True

        if manager is not None and _SESSION_STATUS_RE.search(normalized):
            self.say(
                (
                    f"Den aktuella sessionen är {manager.run_id}."
                    if language == "sv"
                    else f"The current session is {manager.run_id}."
                ),
                turn_started,
            )
            return True

        return original_update_handler(self, command, turn_started)

    def patched_schemas(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        schemas = original_schemas(self, *args, **kwargs)
        schemas.append(
            {
                "type": "function",
                "name": "manage_session_logs",
                "description": (
                    "Inspect, list, export, or open Jarvis per-session "
                    "conversation logs."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "list", "export", "open_folder"],
                        }
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        )
        return schemas

    def patched_call(
        self: Any,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "manage_session_logs":
            manager = _session_manager(self)
            if manager is None:
                raise RuntimeError("Session logging is unavailable.")
            action = str(args["action"])
            if action == "status":
                return {
                    "enabled": manager.enabled,
                    "run_id": manager.run_id,
                    "path": str(manager.run_dir),
                    "conversation_id": manager.current_conversation_id,
                }
            if action == "list":
                return {"sessions": manager.recent_sessions(10)}
            if action == "export":
                path = manager.export_current()
                return {"exported": True, "path": str(path)}
            _open_path(manager.root)
            return {"opened": True, "path": str(manager.root)}
        return original_call(self, name, args)

    def patched_instructions(self: Any) -> str:
        manager = _session_manager(self)
        run_id = manager.run_id if manager is not None else "unavailable"
        return (
            f"{original_instructions(self)}\n\n"
            "SESSION LOGS\n"
            f"- The current Jarvis run has session ID {run_id}.\n"
            "- Every wake conversation has separate readable and JSONL logs.\n"
            "- Use manage_session_logs for status, listing, exporting, or opening logs.\n"
            "- Do not propose session logging as a missing feature; it is implemented.\n"
            "- A simple acknowledgment after a non-question needs no spoken reply.\n"
        )

    Jarvis.run = patched_run
    Jarvis.handle_audio = patched_handle_audio
    Jarvis.transcribe = patched_transcribe
    Jarvis.say = patched_say
    Jarvis.record_followup = patched_followup
    Jarvis._handle_local_update_command = patched_update_handler
    Brain.ask = patched_ask
    Brain.obvious_followup = patched_obvious_followup
    Brain.instructions = patched_instructions
    Tools.schemas = patched_schemas
    Tools.call = patched_call
    _PATCHED = True
