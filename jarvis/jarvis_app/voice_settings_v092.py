from __future__ import annotations

import json
import os
import re
from typing import Any

from .paths import ENV_FILE


_PATCHED = False

ATTR_ENV = {
    "api_key": "OPENAI_API_KEY",
    "stt_model": "STT_MODEL",
    "text_model": "TEXT_MODEL",
    "followup_model": "FOLLOWUP_MODEL",
    "tts_model": "TTS_MODEL",
    "tts_voice": "TTS_VOICE",
    "tts_enabled": "TTS_ENABLED",
    "mic_device": "MIC_DEVICE",
    "speaker_device": "SPEAKER_DEVICE",
    "wake_threshold": "WAKE_THRESHOLD",
    "wake_vad_threshold": "WAKE_VAD_THRESHOLD",
    "wake_cooldown": "WAKE_COOLDOWN_SECONDS",
    "energy_threshold": "ENERGY_THRESHOLD",
    "vad_aggressiveness": "VAD_AGGRESSIVENESS",
    "vad_window_frames": "VAD_WINDOW_FRAMES",
    "vad_min_voiced_frames": "VAD_MIN_VOICED_FRAMES",
    "followup_start_window_frames": "FOLLOWUP_START_WINDOW_FRAMES",
    "followup_start_min_voiced_frames": "FOLLOWUP_START_MIN_VOICED_FRAMES",
    "end_silence": "END_SILENCE_SECONDS",
    "followup_end_silence": "FOLLOWUP_END_SILENCE_SECONDS",
    "followup_timeout": "FOLLOWUP_TIMEOUT_SECONDS",
    "followup_require_intent": "FOLLOWUP_REQUIRE_INTENT",
    "max_utterance": "MAX_UTTERANCE_SECONDS",
    "hard_max_utterance": "HARD_MAX_UTTERANCE_SECONDS",
    "pre_roll": "PRE_ROLL_SECONDS",
    "timezone": "TIMEZONE",
    "max_history": "MAX_HISTORY_MESSAGES",
    "debug": "DEBUG",
    "auto_update_enabled": "AUTO_UPDATE_ENABLED",
    "update_check_interval_seconds": "UPDATE_CHECK_INTERVAL_SECONDS",
    "update_manifest_url": "UPDATE_MANIFEST_URL",
    "update_source_zip_url": "UPDATE_SOURCE_ZIP_URL",
    "skill_planner_alias": "SKILL_PLANNER_ALIAS",
    "skill_planner_model": "SKILL_PLANNER_MODEL",
    "skill_planner_reasoning": "SKILL_PLANNER_REASONING",
    "skill_builder_alias": "SKILL_BUILDER_ALIAS",
    "skill_builder_model": "SKILL_BUILDER_MODEL",
    "skill_builder_reasoning": "SKILL_BUILDER_REASONING",
    "skill_runtime_model": "SKILL_RUNTIME_MODEL",
    "skill_runtime_reasoning": "SKILL_RUNTIME_REASONING",
    "skill_build_retries": "SKILL_BUILD_RETRIES",
    "skill_max_tests": "SKILL_MAX_TESTS",
    "skill_live_tests": "SKILL_LIVE_TESTS",
    "background_task_workers": "BACKGROUND_TASK_WORKERS",
    "ha_url": "HOME_ASSISTANT_URL",
    "ha_token": "HOME_ASSISTANT_TOKEN",
}
ENV_ATTR = {env: attr for attr, env in ATTR_ENV.items()}

EXTRA_DEFAULTS: dict[str, Any] = {
    "TTS_BACKEND": "cloud",
    "SUPERTONIC_VOICE": "M1",
    "SUPERTONIC_STEPS": 8,
    "SUPERTONIC_SPEED": 1.05,
    "SUPERTONIC_MAX_CHUNK_LENGTH": 300,
    "SUPERTONIC_SILENCE_SECONDS": 0.12,
    "SUPERTONIC_CACHE_ITEMS": 256,
    "LOCAL_TTS_VOLUME": 1.0,
    "LOCAL_TTS_SWEDISH_VOICE": "sv_SE-lisa-medium",
    "LOCAL_TTS_KOKORO_VOICE": "bm_george",
    "LOCAL_TTS_KOKORO_LANGUAGE": "en-gb",
    "LOCAL_TTS_KOKORO_SPEED": 1.03,
    "LOCAL_TTS_LENGTH_SCALE": 0.96,
    "FOLLOWUP_MIN_CAPTURE_RMS": 250.0,
    "WAKE_CONFIRM_THRESHOLD": 0.44,
    "WAKE_CONFIRM_HITS": 2,
    "WAKE_CONFIRM_WINDOW_FRAMES": 12,
    "WAKE_ENERGY_WINDOW_FRAMES": 50,
    "WAKE_RECENT_RMS_THRESHOLD": 25.0,
    "WAKE_CLIPPED_MIN_PEAK": 0.90,
    "WAKE_CLIPPED_MIN_HITS": 2,
    "STT_WAKE_ONLY_MAX_RMS": 120.0,
    "STT_WAKE_ONLY_MAX_VAD_RATIO": 0.08,
    "SESSION_LOGGING_ENABLED": True,
    "SESSION_LOG_INCLUDE_TRANSCRIPTS": True,
    "SESSION_LOG_RETENTION_DAYS": 30,
    "SESSION_LOG_MAX_SESSIONS": 100,
    "GITHUB_TOKEN": "",
    "JARVIS_GITHUB_REPOSITORY": "Kootryne/AutoUpdaterTest",
    "JARVIS_GITHUB_BRANCH": "main",
    "SHARED_SKILLS_SYNC_SECONDS": 3600,
}

ALIASES = {
    "follow up window": "FOLLOWUP_TIMEOUT_SECONDS",
    "follow-up window": "FOLLOWUP_TIMEOUT_SECONDS",
    "follow up timeout": "FOLLOWUP_TIMEOUT_SECONDS",
    "followup timeout": "FOLLOWUP_TIMEOUT_SECONDS",
    "stt cutoff": "MAX_UTTERANCE_SECONDS",
    "stt cutoff length": "MAX_UTTERANCE_SECONDS",
    "speech cutoff": "MAX_UTTERANCE_SECONDS",
    "maximum speech length": "MAX_UTTERANCE_SECONDS",
    "max utterance": "MAX_UTTERANCE_SECONDS",
    "hard speech cutoff": "HARD_MAX_UTTERANCE_SECONDS",
    "local voice": "SUPERTONIC_VOICE",
    "local voice quality": "SUPERTONIC_STEPS",
    "local voice speed": "SUPERTONIC_SPEED",
    "tts backend": "TTS_BACKEND",
    "wake sensitivity": "WAKE_THRESHOLD",
    "conversation model": "TEXT_MODEL",
    "speech recognition model": "STT_MODEL",
    "api key": "OPENAI_API_KEY",
    "github token": "GITHUB_TOKEN",
}
SECRETS = {"OPENAI_API_KEY", "GITHUB_TOKEN", "HOME_ASSISTANT_TOKEN"}
RESTART_REQUIRED = {
    "OPENAI_API_KEY", "MIC_DEVICE", "SPEAKER_DEVICE", "WAKE_THRESHOLD",
    "WAKE_VAD_THRESHOLD", "VAD_AGGRESSIVENESS", "ENERGY_THRESHOLD",
    "PRE_ROLL_SECONDS", "DEBUG", "AUTO_UPDATE_ENABLED",
    "UPDATE_CHECK_INTERVAL_SECONDS", "UPDATE_MANIFEST_URL",
    "UPDATE_SOURCE_ZIP_URL", "BACKGROUND_TASK_WORKERS",
    "SESSION_LOGGING_ENABLED", "SESSION_LOG_INCLUDE_TRANSCRIPTS",
    "GITHUB_TOKEN", "JARVIS_GITHUB_REPOSITORY", "JARVIS_GITHUB_BRANCH",
    "SHARED_SKILLS_SYNC_SECONDS", "HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN",
}
RANGES = {
    "FOLLOWUP_TIMEOUT_SECONDS": (0.5, 60.0),
    "MAX_UTTERANCE_SECONDS": (2.0, 300.0),
    "HARD_MAX_UTTERANCE_SECONDS": (2.0, 600.0),
    "SUPERTONIC_STEPS": (5, 12),
    "SUPERTONIC_SPEED": (0.7, 2.0),
    "WAKE_THRESHOLD": (0.05, 1.0),
    "WAKE_VAD_THRESHOLD": (0.0, 1.0),
    "VAD_AGGRESSIVENESS": (0, 3),
    "ENERGY_THRESHOLD": (1, 10000),
    "END_SILENCE_SECONDS": (0.1, 5.0),
    "FOLLOWUP_END_SILENCE_SECONDS": (0.1, 5.0),
}


def _text() -> str:
    return ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""


def _read(name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.*?)\s*$", _text(), re.M)
    return match.group(1) if match else None


def _write(name: str, value: str) -> None:
    lines = _text().splitlines()
    output: list[str] = []
    replaced = False
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
    for line in lines:
        if pattern.match(line):
            if not replaced:
                output.append(f"{name}={value}")
                replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{name}={value}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENV_FILE.with_suffix(".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(ENV_FILE)


def _name(value: str) -> str:
    normal = re.sub(r"[_\-]+", " ", str(value).strip().lower())
    normal = re.sub(r"\s+", " ", normal)
    return ALIASES.get(
        normal,
        re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_"),
    )


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    raise ValueError("Expected true or false.")


def _default(settings: Any, env: str) -> Any:
    attr = ENV_ATTR.get(env)
    if attr is not None:
        return getattr(settings, attr)
    return EXTRA_DEFAULTS.get(env, "")


def _factory_default(settings: Any, env: str) -> Any:
    example = ENV_FILE.parent / ".env.example"
    try:
        data = example.read_text(encoding="utf-8")
        match = re.search(rf"^\s*{re.escape(env)}\s*=\s*(.*?)\s*$", data, re.M)
        if match:
            return _parse(settings, env, match.group(1))
    except Exception:
        pass
    if env in EXTRA_DEFAULTS:
        return EXTRA_DEFAULTS[env]
    return _current(settings, env)


def _parse(settings: Any, env: str, value: Any) -> Any:
    exemplar = _default(settings, env)
    if isinstance(exemplar, bool):
        parsed: Any = _bool(value)
    elif isinstance(exemplar, int) and not isinstance(exemplar, bool):
        parsed = int(float(value))
    elif isinstance(exemplar, float):
        parsed = float(value)
    elif env in {"MIC_DEVICE", "SPEAKER_DEVICE"}:
        text = str(value).strip()
        parsed = None if not text else int(text) if text.lstrip("-").isdigit() else text
    else:
        parsed = str(value).strip()

    if env in RANGES and isinstance(parsed, (int, float)):
        low, high = RANGES[env]
        if not low <= parsed <= high:
            raise ValueError(f"{env} must be between {low} and {high}.")
    if env == "TTS_BACKEND" and parsed not in {"local", "cloud"}:
        raise ValueError("TTS_BACKEND must be local or cloud.")
    if env == "SUPERTONIC_VOICE":
        parsed = str(parsed).upper()
        if parsed not in {f"{sex}{n}" for sex in ("M", "F") for n in range(1, 6)}:
            raise ValueError("SUPERTONIC_VOICE must be M1-M5 or F1-F5.")
    return parsed


def _encoded(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _current(settings: Any, env: str) -> Any:
    attr = ENV_ATTR.get(env)
    if attr is not None:
        return getattr(settings, attr)
    raw = _read(env)
    return _parse(settings, env, raw) if raw is not None else _default(settings, env)


def _shown(env: str, value: Any) -> Any:
    if env in SECRETS:
        return {"configured": bool(value), "value": "[REDACTED]" if value else ""}
    return value


def _known() -> list[str]:
    return sorted(set(ENV_ATTR) | set(EXTRA_DEFAULTS))


def _migrate(settings: Any, logger: Any) -> None:
    migrations = {
        "FOLLOWUP_TIMEOUT_SECONDS": ({"4", "4.0", None}, 5.5, "followup_timeout"),
        "MAX_UTTERANCE_SECONDS": ({"12", "12.0", None}, 25.0, "max_utterance"),
        "HARD_MAX_UTTERANCE_SECONDS": ({"12", "12.0", None}, 30.0, "hard_max_utterance"),
    }
    changed = []
    for env, (old, new, attr) in migrations.items():
        if _read(env) in old:
            _write(env, _encoded(new))
            os.environ[env] = _encoded(new)
            setattr(settings, attr, new)
            changed.append(f"{env}={new}")
    if changed:
        logger.info("SETTINGS | migrated defaults: %s", ", ".join(changed))


def _apply_live(jarvis: Any, env: str, value: Any) -> bool:
    attr = ENV_ATTR.get(env)
    if attr is not None:
        setattr(jarvis.settings, attr, value)
    if env == "TTS_BACKEND":
        jarvis.local_tts_manager.set_backend(str(value))
        return True
    if env in {"SUPERTONIC_VOICE", "SUPERTONIC_STEPS", "SUPERTONIC_SPEED"}:
        jarvis._v090_supertonic.configure(
            voice=value if env == "SUPERTONIC_VOICE" else None,
            steps=value if env == "SUPERTONIC_STEPS" else None,
            speed=value if env == "SUPERTONIC_SPEED" else None,
        )
        return True
    manager = getattr(jarvis.logger, "session_logs", None)
    if manager is not None and env == "SESSION_LOG_RETENTION_DAYS":
        manager.retention_days = int(value)
        manager.cleanup_old_sessions()
        return True
    if manager is not None and env == "SESSION_LOG_MAX_SESSIONS":
        manager.max_sessions = int(value)
        manager.cleanup_old_sessions()
        return True
    return attr is not None and env not in RESTART_REQUIRED


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .tools import Tools

    original_init = Jarvis.__init__
    original_schemas = Tools.schemas
    original_call = Tools.call
    original_instructions = Brain.instructions

    def init(self: Any, settings: Any, config: dict[str, Any], logger: Any) -> None:
        _migrate(settings, logger)
        original_init(self, settings, config, logger)
        self.tools._v092_jarvis = self

    def schemas(self: Any, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        result = [
            schema for schema in original_schemas(self, *args, **kwargs)
            if schema.get("name") != "manage_jarvis_settings"
        ]
        result.append({
            "type": "function",
            "name": "manage_jarvis_settings",
            "description": (
                "List, search, read, change, or reset any Jarvis setting. "
                "Never tell the user to edit a file or run a settings script."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "search", "get", "set", "reset"],
                    },
                    "setting": {"type": ["string", "null"]},
                    "value": {
                        "type": ["string", "number", "integer", "boolean", "null"]
                    },
                },
                "required": ["action", "setting", "value"],
                "additionalProperties": False,
            },
        })
        return result

    def call(self: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != "manage_jarvis_settings":
            return original_call(self, name, args)
        jarvis = getattr(self, "_v092_jarvis", None)
        if jarvis is None:
            raise RuntimeError("Jarvis runtime is unavailable.")
        action = str(args["action"])
        raw = str(args.get("setting") or "")
        env = _name(raw) if raw else None
        known = _known()

        if action == "list":
            return {
                "settings": [
                    {
                        "setting": key,
                        "value": _shown(key, _current(jarvis.settings, key)),
                        "restart_required_for_changes": key in RESTART_REQUIRED,
                        "secret": key in SECRETS,
                    }
                    for key in known
                ]
            }
        if action == "search":
            terms = [v for v in re.split(r"\s+", raw.lower()) if v]
            matches = []
            for key in known:
                phrases = [key.lower().replace("_", " ")]
                phrases.extend(alias for alias, target in ALIASES.items() if target == key)
                if any(all(term in phrase for term in terms) for phrase in phrases):
                    matches.append(key)
            return {"matches": matches[:30]}
        if env not in known:
            return {"found": False, "error": f"Unknown setting: {raw}"}
        if action == "get":
            value = _current(jarvis.settings, env)
            return {
                "found": True,
                "setting": env,
                "value": _shown(env, value),
                "restart_required_for_changes": env in RESTART_REQUIRED,
                "secret": env in SECRETS,
            }
        if action not in {"set", "reset"}:
            raise ValueError(f"Unsupported settings action: {action}")

        before = _current(jarvis.settings, env)
        value = _factory_default(jarvis.settings, env) if action == "reset" else _parse(
            jarvis.settings, env, args.get("value")
        )
        _write(env, _encoded(value))
        os.environ[env] = _encoded(value)
        live = _apply_live(jarvis, env, value)
        jarvis.logger.info(
            "SETTINGS | changed %s | restart=%s | secret=%s",
            env, env in RESTART_REQUIRED, env in SECRETS,
        )
        return {
            "changed": before != value,
            "setting": env,
            "before": _shown(env, before),
            "after": _shown(env, value),
            "applied_live": live,
            "restart_required": env in RESTART_REQUIRED and not live,
            "instruction": (
                "If restart_required is true, say the value is saved and offer "
                "to restart Jarvis through manage_jarvis_process. Never give a "
                "manual setup step."
            ),
        }

    def instructions(self: Brain) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "VOICE-CONFIGURABLE SETTINGS\n"
            "- Use manage_jarvis_settings for every Jarvis configuration question "
            "or change. Never tell the user to edit .env, open a config file, or "
            "run a settings script.\n"
            "- Use search when the spoken setting name is approximate.\n"
            "- Never read secret values aloud. They may be replaced by voice.\n"
            "- If restart_required is true, offer a spoken Jarvis restart and use "
            "manage_jarvis_process after approval.\n"
            "- Defaults: follow-up wait 5.5 seconds, normal request length 25 "
            "seconds, hard recording cutoff 30 seconds.\n"
        )

    Jarvis.__init__ = init
    Tools.schemas = schemas
    Tools.call = call
    Brain.instructions = instructions
    _PATCHED = True
