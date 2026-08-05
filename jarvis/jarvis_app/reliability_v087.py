from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from .language_mode import detect_language
from .paths import APP_DIR, DATA_DIR, ENV_FILE

_PATCHED = False
_POWER_STATE = DATA_DIR / "pending_pc_power.json"

_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|confirm|do it|go ahead|yes please|"
    r"ja|japp|bekräfta|gör det|kör|ja tack)[.!?]*$", re.I
)
_NO_RE = re.compile(
    r"^(?:no|nope|cancel|stop|never mind|not now|"
    r"nej|nä|avbryt|stopp|inte nu|strunt samma)[.!?]*$", re.I
)
_PC_OFF_RE = re.compile(
    r"^(?:please\s+)?(?:shut\s*down|shutdown|turn\s+off|power\s+off)"
    r"\s+(?:(?:my|the)\s+)?(?:pc|computer|windows|machine)(?:\s+now)?[.!?]*$|"
    r"^(?:snälla\s+)?(?:stäng\s+av|slå\s+av)\s+"
    r"(?:(?:min|den här)\s+)?(?:dator|datorn|pc|pc:n|windows)(?:\s+nu)?[.!?]*$", re.I
)
_PC_RESTART_RE = re.compile(
    r"^(?:please\s+)?(?:restart|reboot)\s+"
    r"(?:(?:my|the)\s+)?(?:pc|computer|windows|machine)(?:\s+now)?[.!?]*$|"
    r"^(?:snälla\s+)?starta\s+om\s+"
    r"(?:(?:min|den här)\s+)?(?:dator|datorn|pc|pc:n|windows)(?:\s+nu)?[.!?]*$", re.I
)
_TTS_STATUS_RE = re.compile(
    r"\b(?:are|do)\s+you\s+(?:use|using)\s+(?:the\s+)?"
    r"(?:local|offline|online|cloud|openai).{0,20}\b(?:tts|voice|speech)\b|"
    r"\bare\s+you\s+using\s+local\s+or\s+(?:online|cloud)\s+(?:tts|voice|speech)\b|"
    r"\b(?:which|what)\s+(?:tts|voice|speech)\s+(?:are\s+you\s+using|is\s+active)\b|"
    r"\b(?:tts|voice|speech)\s+status\b|"
    r"\banvänder\s+du\s+(?:lokal|offline|online|openai|moln).{0,20}"
    r"\b(?:tts|röst|talsyntes)\b|"
    r"\banvänder\s+du\s+lokal\s+eller\s+(?:online|moln)\s+(?:tts|röst|talsyntes)\b|"
    r"\bvilken\s+(?:tts|röst|talsyntes)\s+använder\s+du\b", re.I
)
_LOCAL_TTS_RE = re.compile(
    r"\b(?:switch|change|use|enable|turn\s+on)\b.{0,35}"
    r"\b(?:local|offline)\b.{0,15}\b(?:tts|voice|speech)\b|"
    r"\b(?:byt|växla|använd|aktivera|slå\s+på)\b.{0,35}"
    r"\b(?:lokal|offline)\b.{0,15}\b(?:tts|röst|talsyntes)\b", re.I
)
_CLOUD_TTS_RE = re.compile(
    r"\b(?:switch|change|use|enable|turn\s+on)\b.{0,35}"
    r"\b(?:cloud|openai|online)\b.{0,15}\b(?:tts|voice|speech)\b|"
    r"\b(?:byt|växla|använd|aktivera|slå\s+på)\b.{0,35}"
    r"\b(?:moln|openai|online)\b.{0,15}\b(?:tts|röst|talsyntes)\b", re.I
)
_VOICE_BAD_RE = re.compile(
    r"\b(?:this|the|local)?\s*(?:voice|tts|speech|model)"
    r"\s+(?:is|sounds?)\s+(?:so\s+)?(?:bad|awful|terrible|robotic)|"
    r"\b(?:rösten|talsyntesen|den lokala rösten)"
    r"\s+(?:är|låter)\s+(?:jätte)?(?:dålig|hemsk|robotisk)", re.I
)


def _read_power() -> dict[str, Any] | None:
    try:
        value = json.loads(_POWER_STATE.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(str(value["expires_at"]))
        if datetime.now(timezone.utc) >= expires:
            _POWER_STATE.unlink(missing_ok=True)
            return None
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _write_power(action: str, confirmations: int, language: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    value = {
        "action": action,
        "confirmations": confirmations,
        "language": language,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=75)).isoformat(),
    }
    temporary = _POWER_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(_POWER_STATE)


def _power_label(action: str, language: str) -> str:
    if language == "sv":
        return "stänga av datorn" if action == "shutdown" else "starta om datorn"
    return "shut down the PC" if action == "shutdown" else "restart the PC"


def _execute_power(action: str) -> None:
    if sys.platform == "win32":
        command = [
            "shutdown.exe", "/s" if action == "shutdown" else "/r",
            "/t", "5", "/c", "Confirmed twice through Jarvis.",
        ]
    else:
        command = ["shutdown", "-h" if action == "shutdown" else "-r", "now"]
    subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if sys.platform == "win32" else 0
        ),
    )


def _migrate(settings: Any, logger: Any) -> None:
    attrs = {
        "text_model": {"gpt-4.1-mini": "gpt-5.6-luna"},
        "followup_model": {"gpt-4.1-nano": "gpt-5.4-nano"},
        "skill_planner_model": {"gpt-5.2": "gpt-5.6-sol"},
        "skill_builder_model": {"gpt-5.1": "gpt-5.6-luna"},
        "skill_runtime_model": {"gpt-5.1": "gpt-5.6-luna"},
    }
    env_migrations = {
        "TEXT_MODEL": {"gpt-4.1-mini": "gpt-5.6-luna"},
        "FOLLOWUP_MODEL": {"gpt-4.1-nano": "gpt-5.4-nano"},
        "SKILL_PLANNER_MODEL": {"gpt-5.2": "gpt-5.6-sol"},
        "SKILL_BUILDER_MODEL": {"gpt-5.1": "gpt-5.6-luna"},
        "SKILL_RUNTIME_MODEL": {"gpt-5.1": "gpt-5.6-luna"},
        "LOCAL_TTS_SWEDISH_VOICE": {"sv_SE-nst-medium": "sv_SE-lisa-medium"},
    }
    defaults = {
        "LOCAL_TTS_SWEDISH_VOICE": "sv_SE-lisa-medium",
        "LOCAL_TTS_KOKORO_VOICE": "bm_george",
        "LOCAL_TTS_KOKORO_LANGUAGE": "en-gb",
        "LOCAL_TTS_KOKORO_SPEED": "1.03",
    }
    for attribute, migrations in attrs.items():
        current = str(getattr(settings, attribute, "")).strip()
        if current in migrations:
            setattr(settings, attribute, migrations[current])
    try:
        text = ENV_FILE.read_text(encoding="utf-8-sig") if ENV_FILE.exists() else ""
        output, seen, changes = [], set(), []
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                output.append(line)
                continue
            name, value = line.split("=", 1)
            key, current = name.strip(), value.strip()
            seen.add(key)
            replacement = env_migrations.get(key, {}).get(current)
            if replacement:
                output.append(f"{key}={replacement}")
                os.environ[key] = replacement
                changes.append(f"{key}:{current}->{replacement}")
            else:
                output.append(line)
        for key, value in defaults.items():
            if key not in seen:
                output.append(f"{key}={value}")
                os.environ.setdefault(key, value)
                changes.append(f"{key}:added")
        if changes:
            temporary = ENV_FILE.with_suffix(".env.tmp")
            temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
            temporary.replace(ENV_FILE)
            logger.info("MIGRATION | %s", ", ".join(changes))
    except Exception:
        logger.exception("MIGRATION | could not persist 0.8.7 defaults")


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .updater import UpdateManager

    original_init = Jarvis.__init__
    original_handler = Jarvis._handle_local_update_command
    original_instructions = Brain.instructions

    def patched_init(self: Any, settings: Any, config: dict[str, Any], logger: Any) -> None:
        _migrate(settings, logger)
        original_init(self, settings, config, logger)
        self._last_tts_switch_monotonic = 0.0

    def patched_handler(
        self: Any, command: str, turn_started: float | None
    ) -> bool:
        normalized = command.strip()
        language = detect_language(normalized, getattr(self, "current_language", "en"))

        pending = _read_power()
        if pending:
            pending_language = str(pending.get("language") or language)
            if _NO_RE.match(normalized):
                _POWER_STATE.unlink(missing_ok=True)
                self.say("Avbrutet." if pending_language == "sv" else "Cancelled.", turn_started)
                return True
            if _YES_RE.match(normalized):
                action = str(pending["action"])
                if int(pending.get("confirmations", 0)) < 1:
                    _write_power(action, 1, pending_language)
                    label = _power_label(action, pending_language)
                    self.say(
                        f"Bekräfta en gång till: vill du {label} nu?"
                        if pending_language == "sv"
                        else f"Confirm once more: do you want me to {label} now?",
                        turn_started,
                    )
                    return True
                _POWER_STATE.unlink(missing_ok=True)
                self.say(
                    ("Stänger av datorn om fem sekunder." if action == "shutdown"
                     else "Startar om datorn om fem sekunder.")
                    if pending_language == "sv"
                    else ("Shutting down the PC in five seconds." if action == "shutdown"
                          else "Restarting the PC in five seconds."),
                    turn_started,
                )
                _execute_power(action)
                return True
            _POWER_STATE.unlink(missing_ok=True)
            self.logger.info("POWER | pending action cancelled by unrelated command")

        action = (
            "shutdown" if _PC_OFF_RE.match(normalized)
            else "restart" if _PC_RESTART_RE.match(normalized)
            else None
        )
        if action:
            _write_power(action, 0, language)
            label = _power_label(action, language)
            self.say(
                f"Är du säker på att du vill {label}?"
                if language == "sv"
                else f"Are you sure you want me to {label}?",
                turn_started,
            )
            return True

        tts = getattr(self, "local_tts_manager", None)
        if tts and _TTS_STATUS_RE.search(normalized):
            if tts.backend() == "local":
                engine = tts.engine_name(language)
                self.say(
                    f"Jag använder lokal {engine}-talsyntes."
                    if language == "sv"
                    else f"I'm using local {engine} speech.",
                    turn_started,
                )
            else:
                self.say(
                    "Jag använder OpenAI-talsyntes."
                    if language == "sv" else "I'm using OpenAI speech.",
                    turn_started,
                )
            return True

        if tts and _LOCAL_TTS_RE.search(normalized):
            was_local = tts.backend() == "local"
            if not was_local:
                self.say(
                    "Jag förbereder den bättre lokala rösten. Första gången kan ta en stund."
                    if language == "sv"
                    else "I'm preparing the higher-quality local voice. First setup may take a moment.",
                    turn_started,
                )
            try:
                tts.ensure_ready(language)
                tts.set_backend("local")
                self._last_tts_switch_monotonic = time.monotonic()
            except Exception:
                self.logger.exception("LOCAL TTS | high-quality setup failed")
                tts.set_backend("cloud")
                self.say(
                    "Den lokala rösten kunde inte förberedas."
                    if language == "sv" else "The local voice could not be prepared.",
                    None,
                )
                return True
            self.say(
                f"Lokal {tts.engine_name(language)}-talsyntes är aktiv."
                if language == "sv"
                else f"Local {tts.engine_name(language)} speech is active.",
                None if not was_local else turn_started,
            )
            return True

        if tts and _CLOUD_TTS_RE.search(normalized):
            tts.set_backend("cloud")
            self.say(
                "OpenAI-rösten är aktiv."
                if language == "sv" else "The OpenAI voice is active.",
                turn_started,
            )
            return True

        if (
            tts and tts.backend() == "local"
            and _VOICE_BAD_RE.search(normalized)
            and time.monotonic() - float(getattr(self, "_last_tts_switch_monotonic", 0)) < 240
        ):
            self.say(
                "Det är den lokala rösten, inte Luna. Säg använd OpenAI-rösten för att byta tillbaka."
                if language == "sv"
                else "That's the local voice, not Luna. Say use OpenAI TTS to switch back.",
                turn_started,
            )
            return True

        return original_handler(self, command, turn_started)

    def patched_launch_apply(self: Any, *, reason: str) -> bool:
        with self._state_lock:
            if (
                self._apply_launched or self._staged_source is None
                or self._stage_root is None or not self._staged_source.exists()
            ):
                return False
            source_dir, stage_root = self._staged_source, self._stage_root
            target_version = self._staged_version or "unknown"
            self._apply_launched = True
            self._auto_apply_ready = self._manual_apply_ready = False

        helper_source = source_dir / "jarvis_app" / "apply_update_v3.py"
        if not helper_source.is_file():
            self.logger.error("UPDATER | staged v3 helper is missing")
            with self._state_lock:
                self._apply_launched = False
            return False
        helper_copy = stage_root / "apply_update_runner_v3.py"
        shutil.copy2(helper_source, helper_copy)

        result_file = APP_DIR / "data" / "update_result.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.unlink(missing_ok=True)
        restart_mode = os.getenv("JARVIS_LAUNCH_MODE", "background").strip().lower()
        if restart_mode not in {"console", "background"}:
            restart_mode = "background"
        command = [
            sys.executable, str(helper_copy), "--source", str(source_dir),
            "--install", str(APP_DIR), "--parent-pid", str(os.getpid()),
            "--target-version", target_version, "--restart-mode", restart_mode,
        ]
        subprocess.Popen(
            command, cwd=str(stage_root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if sys.platform == "win32" else 0
            ),
        )
        controller = getattr(self, "process_controller", None)
        if controller:
            controller.process_exit_code = 42
            controller.exit_requested = True
        self.logger.info(
            "UPDATER | v3 apply launched | target=%s | reason=%s | mode=%s",
            target_version, reason, restart_mode,
        )
        return True

    def patched_instructions(self: Any) -> str:
        return (
            f"{original_instructions(self)}\n\nPC POWER SAFETY\n"
            "- PC shutdown and restart require two explicit yes confirmations.\n"
            "- A TTS status question reports status and never switches the backend.\n"
            "- Local English speech uses Kokoro; local Swedish speech uses Piper Lisa.\n"
        )

    Jarvis.__init__ = patched_init
    Jarvis._handle_local_update_command = patched_handler
    UpdateManager.launch_apply = patched_launch_apply
    Brain.instructions = patched_instructions
    _PATCHED = True
