from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .paths import APP_DIR


_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .updater import UpdateManager

    def launch_apply(self: Any, *, reason: str) -> bool:
        with self._state_lock:
            if (
                self._apply_launched
                or self._staged_source is None
                or self._stage_root is None
                or not self._staged_source.exists()
            ):
                return False

            source_dir = Path(self._staged_source)
            stage_root = Path(self._stage_root)
            target_version = self._staged_version or "unknown"
            helper_source = source_dir / "jarvis_app" / "apply_update_v3.py"
            if not helper_source.is_file():
                self.logger.error(
                    "UPDATER | staged update helper is missing: %s",
                    helper_source,
                )
                return False

            was_manual = bool(self._manual_apply_ready)
            was_auto = bool(self._auto_apply_ready)
            self._apply_launched = True
            self._auto_apply_ready = False
            self._manual_apply_ready = False

        helper_copy = stage_root / "apply_update_runner_v3.py"
        result_file = APP_DIR / "data" / "update_result.json"
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

        try:
            shutil.copy2(helper_source, helper_copy)
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.unlink(missing_ok=True)

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
        except Exception:
            self.logger.exception("UPDATER | failed to launch staged update helper")
            with self._state_lock:
                self._apply_launched = False
                self._manual_apply_ready = was_manual
                self._auto_apply_ready = was_auto
            return False

        controller = getattr(self, "process_controller", None)
        if controller is not None:
            controller.process_exit_code = 42

        self.logger.info(
            "UPDATER | staged v3 helper launched | target=%s | reason=%s | "
            "restart_mode=%s | helper=%s",
            target_version,
            reason,
            restart_mode,
            helper_source,
        )
        return True

    UpdateManager.launch_apply = launch_apply
    _PATCHED = True
