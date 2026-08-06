from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from .language_mode import detect_language
from .local_stt import STTBackendManager
from .paths import FRAME_MS


_PATCHED = False
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-zÅÄÖåäö]")
_LOCAL_STT_TOPIC_RE = re.compile(
    r"\b(?:stt|speech[- ]to[- ]text|speech recognition|transcription engine|"
    r"parakeet|openai transcrib|local transcription|offline transcription)\b",
    re.IGNORECASE,
)


def _session_manager(jarvis: Any) -> Any | None:
    return getattr(getattr(jarvis, "logger", None), "session_logs", None)


def _record_user_transcript(
    jarvis: Any,
    transcript: str,
    *,
    source: str,
) -> None:
    if not transcript:
        return
    manager = _session_manager(jarvis)
    if manager is not None:
        manager.record_transcript(
            "user",
            transcript,
            source=source,
            language=getattr(jarvis, "current_language", None),
        )


def _wake_only_transcript(jarvis: Any, frames: list[bytes]) -> str | None:
    pre_roll_count = max(
        1,
        int(float(jarvis.settings.pre_roll) * 1000 / FRAME_MS),
    )
    command_frames = frames[min(pre_roll_count, len(frames)) :]
    if not command_frames:
        return None

    measurements = [jarvis.detector.measure(frame) for frame in command_frames]
    max_rms = max(
        (measurement.rms for measurement in measurements),
        default=0.0,
    )
    vad_ratio = (
        sum(int(measurement.vad) for measurement in measurements)
        / len(measurements)
    )
    max_allowed_rms = float(os.getenv("STT_WAKE_ONLY_MAX_RMS", "120"))
    max_allowed_vad = float(
        os.getenv("STT_WAKE_ONLY_MAX_VAD_RATIO", "0.08")
    )
    if max_rms >= max_allowed_rms or vad_ratio > max_allowed_vad:
        return None

    transcript = (
        "Hej Jarvis"
        if getattr(jarvis, "current_language", "en") == "sv"
        else "Hey Jarvis"
    )
    jarvis.logger.info(
        "STT | skipped wake-only local transcription | "
        "max_rms=%.0f vad_ratio=%.1f%% synthetic=%r",
        max_rms,
        vad_ratio * 100,
        transcript,
    )
    return transcript


def _strict_skill_parameters(
    parameters: list[dict[str, Any]],
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in parameters:
        name = str(parameter["name"])
        base_type = str(parameter["type"])
        required_parameter = bool(parameter.get("required"))
        properties[name] = {
            "type": base_type if required_parameter else [base_type, "null"],
            "description": str(parameter.get("description", "")),
        }
        required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from . import voice_settings_v092
    from .app import Jarvis
    from .brain import Brain
    from .skill_schema import SkillRegistry
    from .tools import Tools

    original_init = Jarvis.__init__
    original_run = Jarvis.run
    original_transcribe = Jarvis.transcribe
    original_schemas = Tools.schemas
    original_call = Tools.call
    original_instructions = Brain.instructions
    original_needs_web = Brain.needs_web

    def patched_init(
        self: Any,
        settings: Any,
        config: dict[str, Any],
        logger: Any,
    ) -> None:
        original_init(self, settings, config, logger)
        self._v093_stt = STTBackendManager(logger)
        self.tools._v093_jarvis = self
        logger.info(
            "STT | selected backend=%s | cloud=%s | local=%s",
            self._v093_stt.backend(),
            settings.stt_model,
            self._v093_stt.engine.model_id,
        )

    def patched_run(self: Any) -> None:
        if self._v093_stt.backend() == "local":
            def prewarm() -> None:
                try:
                    started = time.perf_counter()
                    self.logger.info(
                        "LOCAL STT | Parakeet background prewarm started"
                    )
                    self._v093_stt.engine.ensure_ready()
                    self.logger.info(
                        "LOCAL STT | Parakeet background prewarm complete: %.3fs",
                        time.perf_counter() - started,
                    )
                except Exception:
                    self.logger.exception(
                        "LOCAL STT | Parakeet background prewarm failed"
                    )

            threading.Thread(
                target=prewarm,
                name="JarvisParakeetWarmup",
                daemon=True,
            ).start()
        return original_run(self)

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        if self._v093_stt.backend() != "local":
            return original_transcribe(self, frames)
        if not frames:
            return ""

        wake_only = _wake_only_transcript(self, frames)
        if wake_only is not None:
            _record_user_transcript(
                self,
                wake_only,
                source="wake-only-local",
            )
            return wake_only

        total_started = time.perf_counter()
        try:
            transcript = self._v093_stt.engine.transcribe(frames).strip()
        except Exception:
            self.logger.exception(
                "LOCAL STT | Parakeet failed; switching to OpenAI STT"
            )
            self._v093_stt.set_backend("cloud", load=False)
            return original_transcribe(self, frames)

        if (
            transcript
            and _CJK_RE.search(transcript)
            and not _LATIN_RE.search(transcript)
            and getattr(self, "current_language", "en") in {"en", "sv"}
        ):
            self.logger.warning(
                "STT | rejected unexpected non-Latin local transcript: %r",
                transcript,
            )
            transcript = ""

        previous = getattr(self, "current_language", "en")
        self.current_language = detect_language(transcript, previous)
        if hasattr(self, "brain"):
            self.brain.response_language = self.current_language
        self.logger.info(
            "LANGUAGE | detected=%s | transcript=%r",
            self.current_language,
            transcript,
        )
        self.logger.info(
            "TIMING | STT local total: %.3fs",
            time.perf_counter() - total_started,
        )
        _record_user_transcript(
            self,
            transcript,
            source="stt-local-parakeet",
        )
        return transcript

    def patched_schemas(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        schemas = [
            schema
            for schema in original_schemas(self, *args, **kwargs)
            if schema.get("name") != "manage_stt"
        ]
        schemas.append(
            {
                "type": "function",
                "name": "manage_stt",
                "description": (
                    "Inspect or switch Jarvis speech recognition between "
                    "OpenAI cloud transcription and local NVIDIA Parakeet "
                    "TDT v3. The selected backend persists."
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
                                "unload_local",
                            ],
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
        jarvis = getattr(self, "_v093_jarvis", None)
        if name == "manage_stt":
            if jarvis is None:
                raise RuntimeError("The Jarvis STT runtime is unavailable.")
            manager = jarvis._v093_stt
            action = str(args["action"])
            if action == "status":
                return manager.status()
            if action == "use_local":
                started = time.perf_counter()
                status = manager.set_backend("local", load=True)
                status["setup_seconds"] = round(
                    time.perf_counter() - started,
                    3,
                )
                return status
            if action == "use_cloud":
                status = manager.set_backend("cloud", load=False)
                if os.getenv(
                    "PARAKEET_UNLOAD_ON_CLOUD",
                    "false",
                ).strip().lower() in {"1", "true", "yes", "on"}:
                    manager.engine.unload()
                    status = manager.status()
                return status
            if action == "unload_local":
                if manager.backend() == "local":
                    manager.set_backend("cloud", load=False)
                manager.engine.unload()
                return manager.status()
            raise ValueError(f"Unsupported STT action: {action}")

        result = original_call(self, name, args)
        if (
            name == "inspect_jarvis_runtime"
            and isinstance(result, dict)
            and jarvis is not None
        ):
            if isinstance(result.get("models"), dict):
                result["models"]["stt_backend"] = jarvis._v093_stt.backend()
                result["models"]["local_stt"] = jarvis._v093_stt.engine.status()
            else:
                result["stt"] = jarvis._v093_stt.status()
        return result

    def patched_tool_schemas(
        self: SkillRegistry,
    ) -> list[dict[str, Any]]:
        # OpenAI strict function schemas require every property in `required`.
        # Optional skill arguments therefore use a nullable type in the tool
        # schema. Runtime/test validation still uses the original permissive
        # SkillRegistry.parameter_schema.
        schemas: list[dict[str, Any]] = []
        for definition in self.skills.values():
            schemas.append(
                {
                    "type": "function",
                    "name": definition.tool_name,
                    "description": (
                        f"Use the installed Jarvis skill '{definition.name}': "
                        f"{definition.manifest['description']}"
                    ),
                    "strict": True,
                    "parameters": _strict_skill_parameters(
                        list(definition.manifest.get("parameters", []))
                    ),
                }
            )
        return schemas

    def patched_needs_web(self: Any, text: str) -> bool:
        if _LOCAL_STT_TOPIC_RE.search(text):
            return False
        return original_needs_web(self, text)

    def patched_instructions(self: Any) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "SPEECH RECOGNITION BACKEND\n"
            "- Use manage_stt to inspect or switch speech recognition.\n"
            "- 'Local/offline STT' means NVIDIA Parakeet TDT v3. "
            "'OpenAI/online/cloud STT' means the configured OpenAI "
            "transcription model.\n"
            "- Switching does not require restarting Jarvis. The first local "
            "activation downloads and loads the model, so it can take longer.\n"
            "- Do not claim Parakeet is active until manage_stt returns "
            "backend=local.\n\n"
            "PENDING SKILL APPROVAL\n"
            "- When a skill approval is already pending and the user says yes, "
            "possibly with extra non-permission guidance, call "
            "approve_skill_build directly. Do not call build_new_skill again "
            "unless the user explicitly changes the requested capability or "
            "permissions.\n"
        )

    # Expose STT_BACKEND through the general voice settings tool too.
    voice_settings_v092.EXTRA_DEFAULTS["STT_BACKEND"] = "cloud"
    voice_settings_v092.ALIASES.update(
        {
            "stt backend": "STT_BACKEND",
            "speech recognition backend": "STT_BACKEND",
            "transcription backend": "STT_BACKEND",
        }
    )
    original_settings_parse = voice_settings_v092._parse
    original_settings_apply_live = voice_settings_v092._apply_live

    def patched_settings_parse(
        settings: Any,
        env: str,
        value: Any,
    ) -> Any:
        if env == "STT_BACKEND":
            return STTBackendManager._normalise(str(value))
        return original_settings_parse(settings, env, value)

    def patched_settings_apply_live(
        jarvis: Any,
        env: str,
        value: Any,
    ) -> bool:
        if env == "STT_BACKEND":
            jarvis._v093_stt.set_backend(str(value), load=True)
            return True
        return original_settings_apply_live(jarvis, env, value)

    voice_settings_v092._parse = patched_settings_parse
    voice_settings_v092._apply_live = patched_settings_apply_live

    Jarvis.__init__ = patched_init
    Jarvis.run = patched_run
    Jarvis.transcribe = patched_transcribe
    Tools.schemas = patched_schemas
    Tools.call = patched_call
    SkillRegistry.tool_schemas = patched_tool_schemas
    Brain.needs_web = patched_needs_web
    Brain.instructions = patched_instructions
    _PATCHED = True
