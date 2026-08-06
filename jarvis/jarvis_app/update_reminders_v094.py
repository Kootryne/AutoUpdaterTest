from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
from typing import Any

from .language_mode import detect_language
from .paths import DATA_DIR


_PATCHED = False
_STATE_FILE = DATA_DIR / "update_consent.json"

_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|sure|do it|install(?: it)?|go ahead|"
    r"ja|japp|gör det|installera|kör|absolut)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"\b(?:no|nope|not now|later|wait|don't install|do not install|"
    r"nej|inte nu|senare|vänta|installera inte)\b",
    re.IGNORECASE,
)
_DISMISS_RE = re.compile(
    r"\b(?:don't remind me|do not remind me|stop asking|ignore this update|"
    r"fråga inte igen|påminn mig inte|sluta fråga|ignorera den här uppdateringen)\b",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_seconds(name: str, default: int, minimum: int = 60) -> int:
    try:
        return max(minimum, int(float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_state(value: dict[str, Any] | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not value:
        _STATE_FILE.unlink(missing_ok=True)
        return
    temporary = _STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(_STATE_FILE)


def _clear_state(manager: Any) -> None:
    _write_state(None)
    manager._deferred_version = None
    manager._consent_version = None
    manager._consent_prompt_pending = False


def _schedule(
    manager: Any,
    version: str,
    *,
    reason: str,
    dismissed: bool = False,
) -> dict[str, Any]:
    previous = _read_state()
    same_version = str(previous.get("deferred_version") or "") == version
    previous_count = int(previous.get("no_response_count", 0)) if same_version else 0

    if reason in {"no_response", "unclear"}:
        count = previous_count + 1
        base = _env_seconds("UPDATE_NO_RESPONSE_REMINDER_SECONDS", 1800)
        maximum = _env_seconds("UPDATE_REMINDER_MAX_SECONDS", 21600)
        delay = min(maximum, base * (2 ** max(0, count - 1)))
    elif reason == "declined":
        count = previous_count
        delay = _env_seconds("UPDATE_DECLINED_REMINDER_SECONDS", 21600)
    else:
        count = previous_count
        delay = _env_seconds("UPDATE_NO_RESPONSE_REMINDER_SECONDS", 1800)

    now = _utcnow()
    next_at = None if dismissed else now + timedelta(seconds=delay)
    state = {
        "deferred_version": version,
        "reason": reason,
        "dismissed_until_new_version": bool(dismissed),
        "no_response_count": count,
        "next_reminder_at": _iso(next_at) if next_at else None,
        "updated_at": _iso(now),
    }
    _write_state(state)
    manager._deferred_version = version
    manager._consent_version = version
    manager._consent_prompt_pending = False
    return state


def _reminder_due(version: str) -> bool:
    if not _env_bool("UPDATE_REMINDERS_ENABLED", True):
        return False
    state = _read_state()
    saved_version = str(state.get("deferred_version") or "")
    if not saved_version or saved_version != version:
        return True
    if bool(state.get("dismissed_until_new_version")):
        return False
    next_at = _parse_time(state.get("next_reminder_at"))
    return next_at is None or _utcnow() >= next_at


def _raw_apply_ready(manager: Any) -> bool:
    with manager._state_lock:
        return bool(
            manager._auto_apply_ready
            and manager._staged_source
            and manager._staged_source.exists()
            and not manager._apply_launched
        )


def _restore_listening(controller: Any, logger: Any) -> None:
    if controller is None or getattr(controller, "exit_requested", False):
        return
    try:
        wake_model = getattr(controller, "wake_model", None)
        if wake_model is not None:
            wake_model.reset()
    except Exception:
        logger.debug("UPDATER | wake reset after consent prompt failed", exc_info=True)
    try:
        controller.audio.enable()
        logger.info("UPDATER | listening restored after deferred update prompt")
    except Exception:
        logger.exception("UPDATER | failed to restore listening after update prompt")


def _add_voice_settings() -> None:
    try:
        from . import voice_settings_v092 as settings_patch

        settings_patch.EXTRA_DEFAULTS.update(
            {
                "UPDATE_REMINDERS_ENABLED": True,
                "UPDATE_NO_RESPONSE_REMINDER_SECONDS": 1800,
                "UPDATE_DECLINED_REMINDER_SECONDS": 21600,
                "UPDATE_REMINDER_MAX_SECONDS": 21600,
            }
        )
        settings_patch.ALIASES.update(
            {
                "update reminders": "UPDATE_REMINDERS_ENABLED",
                "update reminder": "UPDATE_REMINDERS_ENABLED",
                "update no response reminder": "UPDATE_NO_RESPONSE_REMINDER_SECONDS",
                "update reminder after no answer": "UPDATE_NO_RESPONSE_REMINDER_SECONDS",
                "update declined reminder": "UPDATE_DECLINED_REMINDER_SECONDS",
                "update reminder maximum": "UPDATE_REMINDER_MAX_SECONDS",
            }
        )
        settings_patch.RANGES.update(
            {
                "UPDATE_NO_RESPONSE_REMINDER_SECONDS": (60, 604800),
                "UPDATE_DECLINED_REMINDER_SECONDS": (60, 604800),
                "UPDATE_REMINDER_MAX_SECONDS": (60, 604800),
            }
        )
    except Exception:
        pass


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .updater import UpdateManager

    previous_init = UpdateManager.__init__
    previous_check = UpdateManager.check_and_stage
    previous_manual = UpdateManager.request_manual_update
    previous_status = UpdateManager.status
    previous_instructions = Brain.instructions

    _add_voice_settings()

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        previous_init(self, *args, **kwargs)
        if not hasattr(self, "_consent_lock"):
            self._consent_lock = threading.Lock()
        self._v094_auto_consent_enabled = False
        self._v094_last_prompt_answer = None

    def check_and_stage(
        self: Any,
        *,
        source: str,
        request_auto_apply: bool = False,
        request_manual_apply: bool = False,
    ) -> Any:
        result = previous_check(
            self,
            source=source,
            request_auto_apply=request_auto_apply,
            request_manual_apply=request_manual_apply,
        )
        if request_auto_apply:
            self._v094_auto_consent_enabled = bool(
                result.update_available and result.staged
            )
            if not result.update_available:
                state = _read_state()
                deferred = str(state.get("deferred_version") or "")
                if deferred and deferred in {
                    str(result.current_version or ""),
                    str(result.remote_version or ""),
                }:
                    with self._consent_lock:
                        _clear_state(self)
        if request_auto_apply and result.update_available and result.staged:
            version = str(result.remote_version or self.staged_version or "new")
            with self._consent_lock:
                self._consent_version = version
                if _reminder_due(version):
                    self._consent_prompt_pending = True
            with self._state_lock:
                self._auto_apply_ready = False
        return result

    def request_manual_update(self: Any) -> Any:
        with self._consent_lock:
            _clear_state(self)
            self._v094_auto_consent_enabled = False
        return previous_manual(self)

    def status(self: Any) -> dict[str, Any]:
        payload = previous_status(self)
        state = _read_state()
        payload["update_reminder"] = {
            "enabled": _env_bool("UPDATE_REMINDERS_ENABLED", True),
            "version": state.get("deferred_version"),
            "reason": state.get("reason"),
            "next_reminder_at": state.get("next_reminder_at"),
            "dismissed_until_new_version": bool(
                state.get("dismissed_until_new_version")
            ),
        }
        return payload

    def auto_apply_ready(self: Any) -> bool:
        if _raw_apply_ready(self):
            return True
        if not bool(getattr(self, "_v094_auto_consent_enabled", False)):
            return False

        controller = getattr(self, "process_controller", None)
        if controller is None or getattr(controller, "exit_requested", False):
            return False
        skills = getattr(controller, "skill_system", None)
        if skills is not None and skills.has_active_tasks():
            return False

        version = str(self.staged_version or self.remote_version or "new")
        with self._consent_lock:
            if not self._consent_prompt_pending and _reminder_due(version):
                self._consent_prompt_pending = True
                self._consent_version = version
            if not self._consent_prompt_pending or self._consent_prompt_running:
                return False
            self._consent_prompt_pending = False
            self._consent_prompt_running = True

        installing = False
        try:
            language = getattr(controller, "current_language", "en")
            question = (
                f"Uppdatering {version} är redo. Ska jag installera den nu?"
                if language == "sv"
                else f"Update {version} is ready. Should I install it now?"
            )
            controller.say(question)
            followup = controller.record_followup()

            if followup is None:
                state = _schedule(self, version, reason="no_response")
                self.logger.info(
                    "UPDATER | no consent response; reminder scheduled for %s",
                    state.get("next_reminder_at"),
                )
                return False

            answer = controller.transcribe(followup).strip()
            self._v094_last_prompt_answer = answer
            language = detect_language(answer, language)

            if _DISMISS_RE.search(answer):
                _schedule(self, version, reason="dismissed", dismissed=True)
                controller.say(
                    "Jag frågar inte igen om den här versionen."
                    if language == "sv"
                    else "I won't ask again about this version."
                )
                self.logger.info("UPDATER | reminder dismissed until a newer version")
                return False

            if _YES_RE.search(answer):
                _clear_state(self)
                with self._state_lock:
                    self._auto_apply_ready = True
                controller.say(
                    "Installerar nu." if language == "sv" else "Installing now."
                )
                self.logger.info("UPDATER | spoken consent accepted")
                installing = True
                return True

            if _NO_RE.search(answer):
                state = _schedule(self, version, reason="declined")
                controller.say(
                    "Okej. Jag påminner dig senare."
                    if language == "sv"
                    else "Okay. I'll remind you later."
                )
                self.logger.info(
                    "UPDATER | consent declined; reminder scheduled for %s",
                    state.get("next_reminder_at"),
                )
                return False

            state = _schedule(self, version, reason="unclear")
            controller.say(
                "Jag uppfattade inget tydligt svar. Jag påminner dig senare."
                if language == "sv"
                else "I didn't catch a clear answer. I'll remind you later."
            )
            self.logger.info(
                "UPDATER | unclear consent response %r; reminder scheduled for %s",
                answer,
                state.get("next_reminder_at"),
            )
            return False
        finally:
            with self._consent_lock:
                self._consent_prompt_running = False
            if not installing:
                _restore_listening(controller, self.logger)

    def instructions(self: Any) -> str:
        return (
            f"{previous_instructions(self)}\n\n"
            "UPDATE REMINDERS\n"
            "- If an automatic update prompt receives no answer, Jarvis remains "
            "available and schedules another reminder.\n"
            "- A normal decline snoozes the update. A request not to be reminded "
            "dismisses only that version; a newer version may ask again.\n"
            "- Update reminder intervals and whether reminders are enabled can be "
            "changed through manage_jarvis_settings. Never require file editing.\n"
        )

    UpdateManager.__init__ = init
    UpdateManager.check_and_stage = check_and_stage
    UpdateManager.request_manual_update = request_manual_update
    UpdateManager.status = status
    UpdateManager.auto_apply_ready = auto_apply_ready
    Brain.instructions = instructions
    _PATCHED = True
