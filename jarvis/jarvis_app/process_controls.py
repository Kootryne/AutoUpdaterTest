from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import requests

from .paths import APP_DIR

_PATCHED = False

SHUTDOWN_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"shut(?:\s+yourself|\s+jarvis)?\s+off|"
    r"turn\s+(?:yourself|jarvis)\s+off|"
    r"stop\s+(?:yourself|jarvis)|"
    r"go\s+offline|"
    r"stäng\s+av\s+(?:dig|jarvis)|"
    r"stäng\s+ner\s+(?:dig|jarvis)|"
    r"sluta\s+lyssna"
    r")[.!?]*$",
    re.IGNORECASE,
)
RESTART_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"restart\s+(?:yourself|jarvis)|"
    r"reboot\s+(?:yourself|jarvis)|"
    r"starta\s+om\s+(?:dig|jarvis)|"
    r"start\s+om\s+(?:dig|jarvis)"
    r")[.!?]*$",
    re.IGNORECASE,
)
RELEASE_NOTES_RE = re.compile(
    r"(?:release\s+notes|changelog|what(?:'s|\s+is)\s+new|"
    r"new\s+in\s+(?:this|the\s+latest)\s+update|"
    r"vad\s+är\s+nytt|ändringslogg|nytt\s+i\s+uppdateringen)",
    re.IGNORECASE,
)


def _is_swedish(text: str) -> bool:
    return bool(
        re.search(
            r"\b(stäng|starta|om|dig|vad|är|nytt|uppdateringen|ändringslogg)\b",
            text,
            re.IGNORECASE,
        )
    )


def _local_manifest() -> dict[str, Any]:
    try:
        value = json.loads((APP_DIR / "manifest.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _remote_manifest(controller: Any) -> dict[str, Any]:
    try:
        response = requests.get(
            controller.settings.update_manifest_url,
            params={"_": int(__import__("time").time())},
            timeout=(3.0, 8.0),
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else _local_manifest()
    except Exception:
        controller.logger.exception("PROCESS | release-note fetch failed")
        return _local_manifest()


def _notes_payload(controller: Any, *, latest: bool = True) -> dict[str, Any]:
    manifest = _remote_manifest(controller) if latest else _local_manifest()
    notes = manifest.get("release_notes", {})
    if isinstance(notes, list):
        english = [str(item) for item in notes]
        swedish = english
    elif isinstance(notes, dict):
        english = [str(item) for item in notes.get("en", [])]
        swedish = [str(item) for item in notes.get("sv", english)]
    else:
        english = []
        swedish = []
    return {
        "version": str(manifest.get("version", "unknown")),
        "english": english,
        "swedish": swedish,
    }


def _spoken_notes(controller: Any, *, swedish: bool, latest: bool = True) -> str:
    payload = _notes_payload(controller, latest=latest)
    notes = payload["swedish" if swedish else "english"]
    version = payload["version"]

    print(f"\nRELEASE NOTES {version}")
    for note in notes:
        print(f"- {note}")
    print()

    if not notes:
        return (
            "Jag hittade inga versionsanteckningar."
            if swedish
            else "I couldn't find any release notes."
        )

    short = "; ".join(notes[:3])
    return f"Version {version}: {short}"


def _launch_background_restart(controller: Any) -> None:
    helper_source = APP_DIR / "jarvis_app" / "restart_helper.py"
    command = [
        sys.executable,
        str(helper_source),
        "--parent-pid",
        str(os.getpid()),
        "--install",
        str(APP_DIR),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(
        command,
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    controller.logger.info("PROCESS | detached restart helper launched")


def _request_shutdown(controller: Any) -> None:
    controller.process_exit_code = 43
    controller.exit_requested = True
    controller.logger.info("PROCESS | shutdown requested")


def _request_restart(controller: Any) -> None:
    launch_mode = os.getenv("JARVIS_LAUNCH_MODE", "background").strip().lower()
    if launch_mode != "console":
        _launch_background_restart(controller)
    controller.process_exit_code = 44
    controller.exit_requested = True
    controller.logger.info("PROCESS | restart requested | mode=%s", launch_mode)


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .tools import Tools
    from .updater import UpdateManager

    original_init = Jarvis.__init__
    original_local_update = Jarvis._handle_local_update_command
    original_followup = Jarvis.record_followup
    original_schemas = Tools.schemas
    original_call = Tools.call

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.process_exit_code = 0
        self.tools.process_controller = self
        self.updater.process_controller = self

    def patched_local_update(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        swedish = _is_swedish(normalized)

        if SHUTDOWN_RE.match(normalized):
            self.say("Stänger av." if swedish else "Shutting down.", turn_started)
            _request_shutdown(self)
            return True

        if RESTART_RE.match(normalized):
            self.say("Startar om." if swedish else "Restarting.", turn_started)
            _request_restart(self)
            return True

        if RELEASE_NOTES_RE.search(normalized):
            self.say(
                _spoken_notes(self, swedish=swedish, latest=True),
                turn_started,
            )
            return True

        return original_local_update(self, command, turn_started)

    def patched_followup(self: Any) -> Any:
        if getattr(self, "exit_requested", False):
            return None
        return original_followup(self)

    def patched_schemas(self: Any, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        schemas = original_schemas(self, *args, **kwargs)
        schemas.extend(
            [
                {
                    "type": "function",
                    "name": "manage_jarvis_process",
                    "description": (
                        "Stop or restart Jarvis itself. This controls only the "
                        "Jarvis assistant, not the Windows PC."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["shutdown", "restart"],
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "get_jarvis_release_notes",
                    "description": (
                        "Read Jarvis release notes for the installed version or "
                        "the latest version published in the update manifest."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "which": {
                                "type": "string",
                                "enum": ["current", "latest"],
                            }
                        },
                        "required": ["which"],
                        "additionalProperties": False,
                    },
                },
            ]
        )
        return schemas

    def patched_call(self: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        controller = getattr(self, "process_controller", None)
        if name == "manage_jarvis_process":
            if controller is None:
                raise RuntimeError("Jarvis process controller is unavailable.")
            action = str(args["action"])
            if action == "restart":
                _request_restart(controller)
            else:
                _request_shutdown(controller)
            return {"accepted": True, "action": action}

        if name == "get_jarvis_release_notes":
            if controller is None:
                raise RuntimeError("Jarvis process controller is unavailable.")
            return _notes_payload(
                controller,
                latest=str(args["which"]) == "latest",
            )

        return original_call(self, name, args)

    def patched_launch_apply(self: Any, *, reason: str) -> bool:
        with self._state_lock:
            if (
                self._apply_launched
                or self._staged_source is None
                or self._stage_root is None
                or not self._staged_source.exists()
            ):
                return False

            source_dir = self._staged_source
            stage_root = self._stage_root
            target_version = self._staged_version or "unknown"
            self._apply_launched = True
            self._auto_apply_ready = False
            self._manual_apply_ready = False

        helper_source = APP_DIR / "jarvis_app" / "apply_update_v2.py"
        helper_copy = stage_root / "apply_update_runner.py"
        shutil.copy2(helper_source, helper_copy)

        result_file = APP_DIR / "data" / "update_result.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.unlink(missing_ok=True)

        restart_mode = os.getenv("JARVIS_LAUNCH_MODE", "background").strip().lower()
        if restart_mode not in {"console", "background"}:
            restart_mode = "background"

        command = [
            sys.executable,
            str(helper_copy),
            "--source",
            str(source_dir),
            "--install",
            str(APP_DIR),
            "--parent-pid",
            str(os.getpid()),
            "--target-version",
            target_version,
            "--restart-mode",
            restart_mode,
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        subprocess.Popen(
            command,
            cwd=str(stage_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

        controller = getattr(self, "process_controller", None)
        if controller is not None:
            controller.process_exit_code = 42

        self.logger.info(
            "UPDATER | apply launched | target=%s | reason=%s | restart_mode=%s",
            target_version,
            reason,
            restart_mode,
        )
        return True

    Jarvis.__init__ = patched_init
    Jarvis._handle_local_update_command = patched_local_update
    Jarvis.record_followup = patched_followup
    Tools.schemas = patched_schemas
    Tools.call = patched_call
    UpdateManager.launch_apply = patched_launch_apply
    _PATCHED = True
