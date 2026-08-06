from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

from . import reliability_v082
from .language_mode import detect_language
from .paths import DATA_DIR, FRAME_MS
from .session_logging import SessionLogManager
from .supertonic_tts import SupertonicLocalTTS


_PATCHED = False
_POWER_STATE = DATA_DIR / "pending_pc_power_v090.json"
_LOCAL_JARVIS_TOPIC_RE = re.compile(
    r"\b(?:jarvis|session logs?|conversation logs?|tts|speech engine|"
    r"local voice|installed model|skill status|github connection|"
    r"release notes?|changelog|your latest update|your version)\b",
    re.IGNORECASE,
)


def _session_manager(value: Any) -> SessionLogManager | None:
    logger = value if hasattr(value, "handlers") else getattr(value, "logger", None)
    manager = getattr(logger, "session_logs", None)
    return manager if isinstance(manager, SessionLogManager) else None


def _read_power() -> dict[str, Any] | None:
    try:
        value = json.loads(_POWER_STATE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        expires = datetime.fromisoformat(str(value["expires_at"]))
        if datetime.now(timezone.utc) >= expires:
            _POWER_STATE.unlink(missing_ok=True)
            return None
        return value
    except Exception:
        return None


def _write_power(value: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(value)
    payload["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=90)
    ).isoformat()
    temporary = _POWER_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(_POWER_STATE)


def _execute_power(action: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("PC power control is only enabled on Windows.")
    command = [
        "shutdown.exe",
        "/s" if action == "shutdown" else "/r",
        "/t",
        "5",
        "/c",
        "Confirmed twice through Jarvis Luna.",
    ]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ),
    )


def _runtime_snapshot(tools: Any, topic: str) -> dict[str, Any]:
    jarvis = getattr(tools, "_v090_jarvis", None)
    settings = tools.settings
    session = _session_manager(tools)
    tts_manager = getattr(jarvis, "local_tts_manager", None)
    supertonic = getattr(jarvis, "_v090_supertonic", None)
    skill_system = getattr(tools, "skill_system", None)
    updater = getattr(tools, "updater", None)

    models = {
        "conversation": getattr(settings, "text_model", None),
        "followup": getattr(settings, "followup_model", None),
        "stt": getattr(settings, "stt_model", None),
        "cloud_tts": getattr(settings, "tts_model", None),
        "skill_planner": getattr(settings, "skill_planner_model", None),
        "skill_builder": getattr(settings, "skill_builder_model", None),
        "skill_runtime": getattr(settings, "skill_runtime_model", None),
    }
    tts = {
        "backend": tts_manager.backend() if tts_manager is not None else "unknown",
        "local_engine": "Supertonic 3",
        "voice": getattr(supertonic, "voice", os.getenv("SUPERTONIC_VOICE", "M1")),
        "steps": getattr(supertonic, "steps", int(os.getenv("SUPERTONIC_STEPS", "3"))),
        "speed": getattr(
            supertonic, "speed", float(os.getenv("SUPERTONIC_SPEED", "1.08"))
        ),
    }
    session_data = {
        "enabled": session is not None and session.enabled,
        "run_id": session.run_id if session is not None else None,
        "path": str(session.run_dir) if session is not None else None,
        "conversation_id": (
            session.current_conversation_id if session is not None else None
        ),
    }
    updater_data = updater.status() if updater is not None else {"available": False}
    github = {
        "configured": bool(os.getenv("GITHUB_TOKEN", "").strip()),
        "repository": os.getenv(
            "JARVIS_GITHUB_REPOSITORY", "Kootryne/AutoUpdaterTest"
        ),
    }
    skills: list[dict[str, Any]] = []
    if skill_system is not None:
        manager = getattr(skill_system, "shared_manager", None)
        if manager is not None:
            try:
                skills = list(manager.list_state())
            except Exception:
                tools.logger.exception("INSPECT | could not list skill state")
        else:
            registry = getattr(skill_system, "registry", None)
            definitions = getattr(registry, "skills", {}) if registry is not None else {}
            skills = [
                {"id": skill_id, "name": getattr(definition, "name", skill_id)}
                for skill_id, definition in definitions.items()
            ]
    audio = {
        "microphone_device": getattr(settings, "mic_device", None),
        "speaker_device": getattr(settings, "speaker_device", None),
        "energy_threshold": getattr(settings, "energy_threshold", None),
        "wake_threshold": getattr(settings, "wake_threshold", None),
    }

    sections = {
        "models": models,
        "tts": tts,
        "session": session_data,
        "updater": updater_data,
        "github": github,
        "skills": skills,
        "audio": audio,
    }
    if topic == "overview":
        return sections
    return {topic: sections[topic]}


def _power_tool(tools: Any, args: dict[str, Any]) -> dict[str, Any]:
    operation = str(args.get("operation", "")).strip().lower()
    action = str(args.get("power_action") or "").strip().lower() or None
    turn_id = int(getattr(tools, "_v090_turn_id", 0))

    if operation == "status":
        state = _read_power()
        return {
            "pending": state is not None,
            "power_action": state.get("action") if state else None,
            "confirmations_received": int(state.get("confirmations", 0)) if state else 0,
            "confirmations_required": 2,
        }

    if operation == "cancel":
        existed = _read_power() is not None
        _POWER_STATE.unlink(missing_ok=True)
        return {"cancelled": existed}

    if operation == "request":
        if action not in {"shutdown", "restart"}:
            raise ValueError("power_action must be shutdown or restart.")
        _write_power(
            {
                "action": action,
                "confirmations": 0,
                "last_confirmation_turn": turn_id,
            }
        )
        return {
            "accepted": True,
            "executed": False,
            "power_action": action,
            "confirmation_required": True,
            "confirmations_remaining": 2,
            "instruction": (
                "Ask the user to confirm this exact PC power action. "
                "Do not call confirm until a later user turn."
            ),
        }

    if operation != "confirm":
        raise ValueError(f"Unsupported PC power operation: {operation}")

    state = _read_power()
    if state is None:
        return {
            "accepted": False,
            "executed": False,
            "reason": "There is no pending PC power request.",
        }

    if turn_id <= int(state.get("last_confirmation_turn", -1)):
        return {
            "accepted": False,
            "executed": False,
            "reason": (
                "A confirmation must come from a new user turn. "
                "Ask the user and wait for their reply."
            ),
        }

    confirmations = int(state.get("confirmations", 0)) + 1
    pending_action = str(state["action"])
    if confirmations < 2:
        _write_power(
            {
                "action": pending_action,
                "confirmations": confirmations,
                "last_confirmation_turn": turn_id,
            }
        )
        return {
            "accepted": True,
            "executed": False,
            "power_action": pending_action,
            "confirmation_required": True,
            "confirmations_remaining": 1,
            "instruction": (
                "Ask for one final explicit confirmation and wait for another user turn."
            ),
        }

    _POWER_STATE.unlink(missing_ok=True)
    _execute_power(pending_action)
    return {
        "accepted": True,
        "executed": True,
        "power_action": pending_action,
        "delay_seconds": 5,
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .tools import Tools

    original_init = Jarvis.__init__
    original_say = Jarvis.say
    original_schemas = Tools.schemas
    original_call = Tools.call
    prior_instructions = Brain.instructions

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._v090_supertonic = SupertonicLocalTTS(self.logger)
        self.tools._v090_jarvis = self
        self.brain._v090_turn_id = 0

    def patched_run(self: Any) -> None:
        manager = _session_manager(self)
        tts_state = getattr(self, "local_tts_manager", None)

        if tts_state is not None and tts_state.backend() == "local":
            def prewarm_local_tts() -> None:
                try:
                    started = time.perf_counter()
                    self.logger.info("LOCAL TTS | Supertonic background prewarm started")
                    self._v090_supertonic.ensure_ready()
                    self.logger.info(
                        "LOCAL TTS | Supertonic background prewarm complete: %.3fs",
                        time.perf_counter() - started,
                    )
                except Exception:
                    self.logger.exception("LOCAL TTS | Supertonic prewarm failed")

            threading.Thread(
                target=prewarm_local_tts,
                name="JarvisSupertonicWarmup",
                daemon=True,
            ).start()

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

        self.updater.start()
        audio_started = time.perf_counter()
        self.audio.open()
        self.audio.enable()
        self.logger.info(
            "TIMING | microphone startup: %.3fs",
            time.perf_counter() - audio_started,
        )

        pre_roll_count = max(1, int(self.settings.pre_roll * 1000 / FRAME_MS))
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_count)

        print("\nJARVIS READY | Say 'Hey Jarvis' | Ctrl+C stops it\n")
        self.logger.info(
            "Ready. Wake threshold %.2f | VAD mode=%d | energy=%d | max=%.1fs",
            self.settings.wake_threshold,
            self.settings.vad_aggressiveness,
            self.settings.energy_threshold,
            self.settings.effective_max_utterance,
        )

        try:
            while not self.exit_requested:
                if (
                    self.updater.auto_apply_ready()
                    and not self.skill_system.has_active_tasks()
                ):
                    self.logger.info(
                        "UPDATER | applying staged automatic update while idle"
                    )
                    self.audio.disable()
                    if self.updater.launch_apply(reason="automatic"):
                        self.exit_requested = True
                        break

                try:
                    frame = self.audio.get(timeout=0.25)
                except queue.Empty:
                    continue

                pre_roll.append(frame)
                samples = np.frombuffer(frame, dtype=np.int16)
                scores = self.wake_model.predict(samples)
                score = float(scores.get("hey_jarvis", 0.0))

                if self.settings.debug and score >= 0.08:
                    self.logger.debug("Wake score %.3f", score)

                now = time.monotonic()
                if (
                    score >= self.settings.wake_threshold
                    and now - self.last_wake >= self.settings.wake_cooldown
                ):
                    self.last_wake = now
                    interaction_started = time.perf_counter()
                    self.logger.info("Wake detected, score %.3f", score)
                    frames = self.record_after_wake(list(pre_roll))
                    pre_roll.clear()
                    self.audio.disable()
                    self.handle_audio(frames, interaction_started)
                    if self.exit_requested:
                        break
                    self.wake_model.reset()
                    self.audio.enable()
                    self.logger.info("Listening for wake word...")
        except KeyboardInterrupt:
            self.logger.info("Stopped by Ctrl+C.")
        finally:
            self.updater.stop()
            self.skill_system.shutdown()
            self.audio.disable()
            self.audio.close()
            if manager is not None:
                manager.close(
                    "update"
                    if getattr(self, "process_exit_code", 0) == 42
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
            transcript = self.transcribe(frames).strip()
            wake_match = reliability_v082.WAKE_RE.match(transcript)
            if wake_match is None:
                self.logger.warning(
                    "WAKE | rejected after transcript confirmation | transcript=%r",
                    transcript,
                )
                return

            self.logger.info("WAKE | transcript confirmation passed")
            command = self.strip_wake(transcript)
            print(f"\nYOU: {transcript}")
            self.logger.info("Transcript: %s", transcript)
            if command != transcript:
                self.logger.info(
                    "Cleaned command after wake removal: %s",
                    command,
                )

            if not command:
                followup = self.record_followup()
                if followup is None:
                    return
                command = self.strip_wake(self.transcribe(followup).strip())
                print(f"YOU: {command}")

            first_turn = True
            while command:
                if self.is_stop(command):
                    return

                turn_started = (
                    interaction_started if first_turn else time.perf_counter()
                )
                first_turn = False

                try:
                    answer = self.brain.ask(command)
                except Exception as exc:
                    self.logger.exception("Brain request failed")
                    answer = self.friendly_error(exc)

                self.say(answer, turn_started)
                if self.updater.manual_apply_ready():
                    if self.updater.launch_apply(reason="voice-tool"):
                        self.exit_requested = True
                    return

                followup = self.record_followup()
                if followup is None:
                    return

                heard = self.transcribe(followup).strip()
                print(f"HEARD DURING FOLLOW-UP: {heard}")
                self.logger.info("Follow-up transcript: %s", heard)

                if (
                    self.settings.followup_require_intent
                    and not self.brain.followup_is_for_jarvis(heard)
                ):
                    self.logger.info(
                        "Ignoring follow-up because it was probably not "
                        "addressed to Jarvis."
                    )
                    return

                command = self.strip_wake(heard)
                print(f"YOU: {command}")
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

    def patched_say(
        self: Any,
        text: str,
        turn_started: float | None = None,
    ) -> None:
        if text in {"Yes?", "Ja?"}:
            self.logger.info("CONVERSATION | wake-only prompt kept silent")
            return

        backend = getattr(self, "local_tts_manager", None)
        if (
            backend is None
            or backend.backend() != "local"
            or not self.settings.tts_enabled
        ):
            original_say(self, text, turn_started)
            return

        speech = self.clean_speech(text)[:1800]
        if not speech:
            return
        language = detect_language(
            text, getattr(self, "current_language", "en")
        )

        manager = _session_manager(self)
        if manager is not None:
            manager.record_transcript(
                "assistant",
                text,
                source="jarvis",
                language=language,
            )

        print(f"JARVIS: {text}\n")
        self.logger.info("Jarvis: %s", text)
        total_started = time.perf_counter()
        self.audio.speaking.set()
        self.audio.disable()

        try:
            audio, rate, cached, synth_elapsed = self._v090_supertonic.synthesize(
                speech,
                language,
            )
            duration = len(audio) / rate if rate else 0.0
            self.logger.info(
                "LOCAL TTS | Supertonic synthesis %.3fs | cached=%s | "
                "chars=%d | audio=%.2fs | rtf=%.3f",
                synth_elapsed,
                cached,
                len(speech),
                duration,
                synth_elapsed / duration if duration else 0.0,
            )
            if turn_started is not None:
                self.logger.info(
                    "TIMING | turn until local playback starts: %.3fs",
                    time.perf_counter() - turn_started,
                )
            playback_started = time.perf_counter()
            sd.play(
                audio,
                rate,
                device=self.settings.speaker_device,
                blocking=True,
            )
            self.logger.info(
                "TIMING | local TTS playback: %.3fs | audio=%.2fs",
                time.perf_counter() - playback_started,
                duration,
            )
        except Exception:
            self.logger.exception(
                "LOCAL TTS | Supertonic failed; switching to cloud"
            )
            try:
                backend.set_backend("cloud")
            except Exception:
                pass
            self.audio.speaking.clear()
            self.audio.flush()
            original_say(self, text, turn_started)
            return
        finally:
            self.logger.info(
                "TIMING | local TTS total: %.3fs",
                time.perf_counter() - total_started,
            )
            time.sleep(0.10)
            self.audio.speaking.clear()
            self.audio.flush()

    def patched_needs_web(self: Any, text: str) -> bool:
        if _LOCAL_JARVIS_TOPIC_RE.search(text):
            return False
        return bool(self.CURRENT_RE.search(text))

    def patched_ask(self: Any, text: str) -> str:
        total_started = time.perf_counter()
        self._v090_turn_id = int(getattr(self, "_v090_turn_id", 0)) + 1
        self.tools._v090_turn_id = self._v090_turn_id

        user_item = {"role": "user", "content": text}
        request_input: list[Any] = [*self.history, user_item]
        include_web = self.needs_web(text)
        include_time = self.needs_time(text)
        schemas = self.tools.schemas(
            include_web=include_web,
            include_time=include_time,
        )
        self.logger.info(
            "ROUTING | web=%s | time=%s | model=%s | followup_model=%s",
            include_web,
            include_time,
            self.settings.text_model,
            self.settings.followup_model,
        )

        response = self.create_response(
            "brain API round 1",
            model=self.settings.text_model,
            instructions=self.instructions(),
            input=request_input,
            tools=schemas,
            max_output_tokens=512,
            store=False,
        )

        rounds = 1
        tool_call_count = 0
        completed_calls: set[str] = set()
        for _ in range(6):
            calls = [
                item
                for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                break

            tool_call_count += len(calls)
            request_input.extend(self.dump_item(item) for item in response.output)
            outputs: list[dict[str, Any]] = []

            for call in calls:
                signature = hashlib.sha256(
                    f"{call.name}|{call.arguments or ''}".encode("utf-8")
                ).hexdigest()
                if signature in completed_calls:
                    payload = {
                        "ok": False,
                        "error": "This identical tool call already ran in this turn.",
                    }
                else:
                    completed_calls.add(signature)
                    try:
                        args = json.loads(call.arguments or "{}")
                        result = self.tools.call(call.name, args)
                        payload = {"ok": True, "result": result}
                    except Exception as exc:
                        self.logger.exception("Tool failed: %s", call.name)
                        payload = {"ok": False, "error": str(exc)}

                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(payload, ensure_ascii=False),
                    }
                )

            request_input.extend(outputs)
            rounds += 1
            response = self.create_response(
                f"brain API round {rounds}",
                model=self.settings.text_model,
                instructions=self.instructions(),
                input=request_input,
                tools=schemas,
                max_output_tokens=512,
                store=False,
            )
        else:
            raise RuntimeError("Too many tool-call rounds.")

        answer = (response.output_text or "").strip()
        if not answer:
            answer = "The action finished, but I could not generate a spoken reply."

        self.history.extend(
            [user_item, {"role": "assistant", "content": answer}]
        )
        self.history = self.history[-self.settings.max_history :]
        self.logger.info(
            "TIMING | brain total: %.3fs | rounds=%d | function_calls=%d",
            time.perf_counter() - total_started,
            rounds,
            tool_call_count,
        )
        return answer

    def final_instructions(self: Any) -> str:
        return (
            f"{prior_instructions(self)}\n\n"
            "MODEL-DRIVEN CONTROL\n"
            "- Every user-facing reply must be written by Luna. Do not rely on "
            "hard-coded local replies or claim state from memory.\n"
            "- Use inspect_jarvis_runtime, manage_session_logs, manage_tts, "
            "update_jarvis, skill tools, and other available tools before "
            "answering questions about Jarvis state.\n"
            "- Questions about Jarvis releases, internal models, TTS, sessions, "
            "skills, GitHub connection, or update status are local tool questions, "
            "not web-search questions.\n"
            "- Do not read long machine IDs aloud unless the user explicitly asks "
            "for the exact ID. Summarize them naturally.\n\n"
            "PC POWER CONTROL\n"
            "- Shutdown and restart are controlled through pc_power_control.\n"
            "- On an initial request, call operation=request with the exact action, "
            "then ask for confirmation.\n"
            "- A later yes means call operation=confirm. If one confirmation remains, "
            "ask once more and wait for another user turn.\n"
            "- Only say the PC is shutting down or restarting when the tool returns "
            "executed=true.\n"
            "- A no or cancellation means call operation=cancel.\n"
            "- Never perform both confirmations in one user turn.\n"
        )

    def patched_schemas(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        schemas = original_schemas(self, *args, **kwargs)
        names = {schema.get("name") for schema in schemas}

        if "inspect_jarvis_runtime" not in names:
            schemas.append(
                {
                    "type": "function",
                    "name": "inspect_jarvis_runtime",
                    "description": (
                        "Read authoritative current Jarvis runtime state. Use this "
                        "instead of guessing about models, TTS, sessions, updater, "
                        "GitHub configuration, skills, or audio configuration."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "topic": {
                                "type": "string",
                                "enum": [
                                    "overview",
                                    "models",
                                    "tts",
                                    "session",
                                    "updater",
                                    "github",
                                    "skills",
                                    "audio",
                                ],
                            }
                        },
                        "required": ["topic"],
                        "additionalProperties": False,
                    },
                }
            )

        if "manage_tts" not in names:
            schemas.append(
                {
                    "type": "function",
                    "name": "manage_tts",
                    "description": (
                        "Inspect, switch, or benchmark Jarvis speech. Local speech "
                        "uses Supertonic 3 for both English and Swedish."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": [
                                    "status",
                                    "use_local",
                                    "use_cloud",
                                    "benchmark",
                                ],
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }
            )

        if "pc_power_control" not in names:
            schemas.append(
                {
                    "type": "function",
                    "name": "pc_power_control",
                    "description": (
                        "Request, confirm, cancel, or inspect a Windows PC shutdown "
                        "or restart. Two confirmations on separate user turns are "
                        "required before execution."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": ["request", "confirm", "cancel", "status"],
                            },
                            "power_action": {
                                "type": ["string", "null"],
                                "enum": ["shutdown", "restart", None],
                            },
                        },
                        "required": ["operation", "power_action"],
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
        if name == "inspect_jarvis_runtime":
            return _runtime_snapshot(self, str(args["topic"]))

        if name == "manage_tts":
            action = str(args["action"])
            jarvis = getattr(self, "_v090_jarvis", None)
            if jarvis is None:
                raise RuntimeError("The Jarvis runtime is unavailable.")
            backend = getattr(jarvis, "local_tts_manager", None)
            engine = jarvis._v090_supertonic

            if action == "status":
                return _runtime_snapshot(self, "tts")["tts"]
            if action == "use_cloud":
                backend.set_backend("cloud")
                return {"backend": "cloud", "engine": self.settings.tts_model}
            if action == "use_local":
                started = time.perf_counter()
                engine.ensure_ready()
                backend.set_backend("local")
                return {
                    "backend": "local",
                    "engine": "Supertonic 3",
                    "voice": engine.voice,
                    "setup_seconds": round(time.perf_counter() - started, 3),
                }
            if action == "benchmark":
                sample = "Jarvis local speech benchmark."
                started = time.perf_counter()
                audio, rate, cached, synth_elapsed = engine.synthesize(sample, "en")
                duration = len(audio) / rate if rate else 0.0
                return {
                    "engine": "Supertonic 3",
                    "cached": cached,
                    "synthesis_seconds": round(synth_elapsed, 3),
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "audio_seconds": round(duration, 3),
                    "real_time_factor": (
                        round(synth_elapsed / duration, 3) if duration else None
                    ),
                    "steps": engine.steps,
                }
            raise ValueError(f"Unsupported TTS action: {action}")

        if name == "pc_power_control":
            return _power_tool(self, args)

        return original_call(self, name, args)

    Jarvis.__init__ = patched_init
    Jarvis.run = patched_run
    Jarvis.handle_audio = patched_handle_audio
    Jarvis.say = patched_say
    Jarvis._handle_local_update_command = lambda self, command, turn_started: False
    Jarvis._handle_local_skill_command = lambda self, command, turn_started: False
    Brain.needs_web = patched_needs_web
    Brain.ask = patched_ask
    Brain.instructions = final_instructions
    Tools.schemas = patched_schemas
    Tools.call = patched_call
    _PATCHED = True
