from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from typing import Any

from .language_mode import detect_language
from .paths import DATA_DIR

_PATCHED = False
_CONSENT_FILE = DATA_DIR / "update_consent.json"


def _read_deferred_version() -> str | None:
    try:
        value = json.loads(_CONSENT_FILE.read_text(encoding="utf-8"))
        return str(value.get("deferred_version")) or None
    except Exception:
        return None


def _write_deferred_version(version: str | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if version is None:
        _CONSENT_FILE.unlink(missing_ok=True)
        return
    temp = _CONSENT_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps({"deferred_version": version}, indent=2),
        encoding="utf-8",
    )
    temp.replace(_CONSENT_FILE)


def _is_yes(text: str) -> bool:
    return bool(
        re.search(
            r"\b(yes|yeah|yep|sure|do it|install it|go ahead|ja|japp|"
            r"gör det|installera|kör|absolut)\b",
            text,
            re.IGNORECASE,
        )
    )


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .updater import UpdateManager

    original_init = UpdateManager.__init__
    original_check = UpdateManager.check_and_stage
    original_auto_ready = UpdateManager.auto_apply_ready
    original_manual = UpdateManager.request_manual_update

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._consent_lock = threading.Lock()
        self._consent_prompt_pending = False
        self._consent_prompt_running = False
        self._consent_version = None
        self._deferred_version = _read_deferred_version()

    def patched_check(
        self: Any,
        *,
        source: str,
        request_auto_apply: bool = False,
        request_manual_apply: bool = False,
    ) -> Any:
        result = original_check(
            self,
            source=source,
            request_auto_apply=False if request_auto_apply else False,
            request_manual_apply=request_manual_apply,
        )
        if request_auto_apply and result.update_available and result.staged:
            version = result.remote_version
            with self._consent_lock:
                if version and version != self._deferred_version:
                    self._consent_version = version
                    self._consent_prompt_pending = True
            with self._state_lock:
                self._auto_apply_ready = False
            self.logger.info(
                "UPDATER | version %s staged; waiting for spoken consent",
                version,
            )
        return result

    def patched_manual(self: Any) -> Any:
        self._deferred_version = None
        _write_deferred_version(None)
        return original_manual(self)

    def patched_auto_ready(self: Any) -> bool:
        if original_auto_ready(self):
            return True

        controller = getattr(self, "process_controller", None)
        if controller is None or getattr(controller, "exit_requested", False):
            return False
        skills = getattr(controller, "skill_system", None)
        if skills is not None and skills.has_active_tasks():
            return False

        with self._consent_lock:
            if not self._consent_prompt_pending or self._consent_prompt_running:
                return False
            self._consent_prompt_pending = False
            self._consent_prompt_running = True
            version = self._consent_version or self.remote_version or "new"

        try:
            language = getattr(controller, "current_language", "en")
            if language == "sv":
                question = f"Jag fick uppdatering {version}. Ska jag installera den?"
            else:
                question = f"I received update {version}. Should I install it?"
            controller.say(question)

            followup = controller.record_followup()
            if followup is None:
                self._deferred_version = str(version)
                _write_deferred_version(str(version))
                self.logger.info("UPDATER | no consent response; update deferred")
                return False

            answer = controller.transcribe(followup).strip()
            language = detect_language(answer, language)
            if _is_yes(answer):
                self._deferred_version = None
                _write_deferred_version(None)
                with self._state_lock:
                    self._auto_apply_ready = True
                controller.say(
                    "Installerar nu." if language == "sv" else "Installing now."
                )
                self.logger.info("UPDATER | spoken consent accepted")
                return True

            self._deferred_version = str(version)
            _write_deferred_version(str(version))
            controller.say(
                "Okej, jag väntar." if language == "sv" else "Okay, I'll wait."
            )
            self.logger.info("UPDATER | spoken consent declined or unclear")
            return False
        finally:
            with self._consent_lock:
                self._consent_prompt_running = False

    UpdateManager.__init__ = patched_init
    UpdateManager.check_and_stage = patched_check
    UpdateManager.request_manual_update = patched_manual
    UpdateManager.auto_apply_ready = patched_auto_ready
    _PATCHED = True
