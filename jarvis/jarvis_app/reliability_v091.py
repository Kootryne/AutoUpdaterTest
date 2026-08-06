from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

import sounddevice as sd
import soundfile as sf

from .language_mode import detect_language


_PATCHED = False
_SAMPLE_TEXT = {
    "en": "Good evening, Viktor. All systems are ready.",
    "sv": "God kväll Viktor. Alla system är redo.",
}


def _open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .tools import Tools

    original_schemas = Tools.schemas
    original_call = Tools.call
    original_instructions = Brain.instructions

    def patched_schemas(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        schemas = original_schemas(self, *args, **kwargs)
        replacement = {
            "type": "function",
            "name": "manage_tts",
            "description": (
                "Inspect and configure Jarvis speech. Use this for complaints about "
                "voice quality, speed, local speech, or choosing a different voice. "
                "Supertonic quality steps must be 5-12; 8 is the balanced default."
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
                            "list_voices",
                            "configure",
                            "preview",
                            "create_voice_pack",
                        ],
                    },
                    "voice": {
                        "type": ["string", "null"],
                        "enum": [
                            "M1", "M2", "M3", "M4", "M5",
                            "F1", "F2", "F3", "F4", "F5", None,
                        ],
                    },
                    "steps": {
                        "type": ["integer", "null"],
                        "minimum": 5,
                        "maximum": 12,
                    },
                    "speed": {
                        "type": ["number", "null"],
                        "minimum": 0.7,
                        "maximum": 2.0,
                    },
                    "language": {
                        "type": ["string", "null"],
                        "enum": ["en", "sv", None],
                    },
                },
                "required": ["action", "voice", "steps", "speed", "language"],
                "additionalProperties": False,
            },
        }

        replaced = False
        for index, schema in enumerate(schemas):
            if schema.get("name") == "manage_tts":
                schemas[index] = replacement
                replaced = True
                break
        if not replaced:
            schemas.append(replacement)
        return schemas

    def patched_call(
        self: Any,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name != "manage_tts":
            return original_call(self, name, args)

        jarvis = getattr(self, "_v090_jarvis", None)
        if jarvis is None:
            raise RuntimeError("The Jarvis runtime is unavailable.")
        backend = getattr(jarvis, "local_tts_manager", None)
        engine = getattr(jarvis, "_v090_supertonic", None)
        if backend is None or engine is None:
            raise RuntimeError("The TTS runtime is unavailable.")

        action = str(args["action"])
        voice = args.get("voice")
        steps = args.get("steps")
        speed = args.get("speed")
        language = str(
            args.get("language")
            or getattr(jarvis, "current_language", "en")
            or "en"
        )
        language = "sv" if language == "sv" else "en"

        if action == "status":
            return {
                "backend": backend.backend(),
                **engine.settings(),
                "cloud_model": self.settings.tts_model,
            }

        if action == "list_voices":
            return {
                "voices": list(engine.VOICES),
                "selected_voice": engine.voice,
                "quality_steps": engine.steps,
                "balanced_steps": 8,
            }

        if action == "use_cloud":
            backend.set_backend("cloud")
            return {
                "backend": "cloud",
                "engine": self.settings.tts_model,
            }

        if action == "use_local":
            started = time.perf_counter()
            engine.ensure_ready()
            backend.set_backend("local")
            return {
                "backend": "local",
                **engine.settings(),
                "setup_seconds": round(time.perf_counter() - started, 3),
            }

        if action == "configure":
            result = engine.configure(
                voice=str(voice) if voice is not None else None,
                steps=int(steps) if steps is not None else None,
                speed=float(speed) if speed is not None else None,
            )
            backend.set_backend("local")
            return {
                "configured": True,
                "backend": "local",
                **result,
            }

        if action == "benchmark":
            sample = _SAMPLE_TEXT[language]
            started = time.perf_counter()
            audio, rate, cached, synth_elapsed = engine.synthesize(
                sample,
                language,
            )
            duration = len(audio) / rate if rate else 0.0
            return {
                "engine": "Supertonic 3",
                "voice": engine.voice,
                "steps": engine.steps,
                "speed": engine.speed,
                "cached": cached,
                "synthesis_seconds": round(synth_elapsed, 3),
                "wall_seconds": round(time.perf_counter() - started, 3),
                "audio_seconds": round(duration, 3),
                "real_time_factor": (
                    round(synth_elapsed / duration, 3) if duration else None
                ),
            }

        if action == "preview":
            selected = str(voice or engine.voice).upper()
            preview_steps = int(steps) if steps is not None else engine.steps
            preview_speed = float(speed) if speed is not None else engine.speed
            audio, rate, cached, synth_elapsed = engine.synthesize_preview(
                _SAMPLE_TEXT[language],
                language,
                voice=selected,
                steps=preview_steps,
                speed=preview_speed,
            )
            sd.play(
                audio,
                rate,
                device=self.settings.speaker_device,
                blocking=True,
            )
            return {
                "previewed": True,
                "voice": selected,
                "steps": preview_steps,
                "speed": preview_speed,
                "language": language,
                "cached": cached,
                "synthesis_seconds": round(synth_elapsed, 3),
            }

        if action == "create_voice_pack":
            desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
            destination = desktop / "Jarvis Voice Samples"
            destination.mkdir(parents=True, exist_ok=True)
            preview_steps = int(steps) if steps is not None else max(8, engine.steps)
            preview_speed = float(speed) if speed is not None else engine.speed
            generated: list[str] = []
            for candidate in engine.VOICES:
                audio, rate, _, _ = engine.synthesize_preview(
                    _SAMPLE_TEXT[language],
                    language,
                    voice=candidate,
                    steps=preview_steps,
                    speed=preview_speed,
                )
                path = destination / (
                    f"{candidate}_{language}_steps-{preview_steps}.wav"
                )
                sf.write(path, audio, rate)
                generated.append(str(path))
            _open_folder(destination)
            return {
                "created": True,
                "folder": str(destination),
                "files": generated,
                "language": language,
                "steps": preview_steps,
                "speed": preview_speed,
            }

        raise ValueError(f"Unsupported TTS action: {action}")

    def patched_instructions(self: Any) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "LOCAL VOICE QUALITY\n"
            "- When the user says the local voice sounds bad, use manage_tts status "
            "instead of guessing.\n"
            "- Supertonic quality below 5 is invalid for normal use. Use 8 as the "
            "balanced default unless the user explicitly chooses another value.\n"
            "- Use preview to play one candidate voice without changing settings.\n"
            "- Use create_voice_pack when the user wants to compare every built-in "
            "voice, then configure only after they choose one.\n"
            "- Never claim a voice or quality setting changed unless manage_tts "
            "confirms it.\n"
        )

    Tools.schemas = patched_schemas
    Tools.call = patched_call
    Brain.instructions = patched_instructions
    _PATCHED = True
