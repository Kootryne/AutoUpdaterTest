from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import zipfile

import requests

from .paths import APP_DIR
from .settings import Settings


VERSION_FILE = APP_DIR / "version.json"
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    "Kootryne/AutoUpdaterTest/main/jarvis/manifest.json"
)
DEFAULT_SOURCE_ZIP_URL = (
    "https://github.com/"
    "Kootryne/AutoUpdaterTest/archive/refs/heads/main.zip"
)
REPOSITORY_FOLDER = "AutoUpdaterTest-main"
PROJECT_FOLDER = "jarvis"


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(numbers or [0])


def read_local_version() -> str:
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        return str(data.get("version", "0.0.0"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return "0.0.0"


@dataclass(slots=True)
class UpdateResult:
    current_version: str
    remote_version: str | None
    update_available: bool
    staged: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "remote_version": self.remote_version,
            "update_available": self.update_available,
            "staged": self.staged,
            "error": self.error,
        }


class UpdateManager:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

        self.current_version = read_local_version()
        self.remote_version: str | None = None
        self.last_error: str | None = None
        self.last_check_monotonic = 0.0

        self._check_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._stage_root: Path | None = None
        self._staged_source: Path | None = None
        self._staged_version: str | None = None
        self._auto_apply_ready = False
        self._manual_apply_ready = False
        self._apply_launched = False

    @property
    def enabled(self) -> bool:
        return self.settings.auto_update_enabled

    @property
    def staged_version(self) -> str | None:
        with self._state_lock:
            return self._staged_version

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="JarvisUpdateMonitor",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "UPDATER | started | current=%s | interval=%ss",
            self.current_version,
            self.settings.update_check_interval_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def _monitor_loop(self) -> None:
        source = "startup"
        while not self._stop_event.is_set():
            try:
                self.check_and_stage(source=source, request_auto_apply=True)
            except Exception:
                self.logger.exception("UPDATER | background check crashed")

            source = "hourly"
            if self._stop_event.wait(self.settings.update_check_interval_seconds):
                break

    def _fetch_remote_manifest(self) -> dict[str, Any]:
        response = requests.get(
            self.settings.update_manifest_url,
            params={"_": int(time.time())},
            timeout=(3.0, 8.0),
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        manifest = response.json()
        if not isinstance(manifest, dict):
            raise RuntimeError("Remote update manifest is not a JSON object.")
        if not manifest.get("version"):
            raise RuntimeError("Remote update manifest has no version.")
        return manifest

    def _stage_download(
        self,
        expected_version: str,
        remote_manifest: dict[str, Any],
    ) -> tuple[Path, Path]:
        stage_root = Path(tempfile.mkdtemp(prefix="jarvis_update_"))
        zip_path = stage_root / "source.zip"
        extract_path = stage_root / "source"

        try:
            with requests.get(
                self.settings.update_source_zip_url,
                params={"_": int(time.time())},
                stream=True,
                timeout=(5.0, 35.0),
                headers={"Cache-Control": "no-cache"},
            ) as response:
                response.raise_for_status()
                with zip_path.open("wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            target.write(chunk)

            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extract_path)

            source_dir = extract_path / REPOSITORY_FOLDER / PROJECT_FOLDER
            source_manifest_path = source_dir / "manifest.json"
            if not source_manifest_path.exists():
                raise RuntimeError("Downloaded source does not contain manifest.json.")

            source_manifest = json.loads(
                source_manifest_path.read_text(encoding="utf-8")
            )
            source_version = str(source_manifest.get("version", ""))
            if source_version != expected_version:
                raise RuntimeError(
                    f"Manifest/source version mismatch: "
                    f"{expected_version} versus {source_version}."
                )

            remote_files = set(remote_manifest.get("managed_files", []))
            source_files = set(source_manifest.get("managed_files", []))
            if remote_files != source_files:
                raise RuntimeError("Downloaded source manifest differs from remote manifest.")

            for relative in source_files:
                relative_path = Path(str(relative))
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise RuntimeError(f"Unsafe update path: {relative}")
                if not (source_dir / relative_path).is_file():
                    raise RuntimeError(f"Update file is missing: {relative}")

            return stage_root, source_dir
        except Exception:
            shutil.rmtree(stage_root, ignore_errors=True)
            raise

    def check_and_stage(
        self,
        *,
        source: str,
        request_auto_apply: bool = False,
        request_manual_apply: bool = False,
    ) -> UpdateResult:
        started = time.perf_counter()
        if not self.enabled:
            return UpdateResult(
                current_version=self.current_version,
                remote_version=None,
                update_available=False,
                staged=False,
                error="Automatic updates are disabled.",
            )

        with self._check_lock:
            try:
                manifest = self._fetch_remote_manifest()
                remote_version = str(manifest["version"])
                self.remote_version = remote_version
                self.last_check_monotonic = time.monotonic()

                newer = version_tuple(remote_version) > version_tuple(
                    self.current_version
                )
                if not newer:
                    self.last_error = None
                    self.logger.info(
                        "UPDATER | check=%s | current=%s | remote=%s | up-to-date",
                        source,
                        self.current_version,
                        remote_version,
                    )
                    return UpdateResult(
                        current_version=self.current_version,
                        remote_version=remote_version,
                        update_available=False,
                        staged=False,
                    )

                with self._state_lock:
                    already_staged = (
                        self._staged_source is not None
                        and self._staged_version == remote_version
                        and self._staged_source.exists()
                    )
                    if already_staged:
                        if request_auto_apply:
                            self._auto_apply_ready = True
                        if request_manual_apply:
                            self._manual_apply_ready = True
                        return UpdateResult(
                            current_version=self.current_version,
                            remote_version=remote_version,
                            update_available=True,
                            staged=True,
                        )

                download_started = time.perf_counter()
                stage_root, source_dir = self._stage_download(
                    remote_version,
                    manifest,
                )

                with self._state_lock:
                    old_stage = self._stage_root
                    self._stage_root = stage_root
                    self._staged_source = source_dir
                    self._staged_version = remote_version
                    self._auto_apply_ready = request_auto_apply
                    self._manual_apply_ready = request_manual_apply
                    self.last_error = None

                if old_stage and old_stage != stage_root:
                    shutil.rmtree(old_stage, ignore_errors=True)

                self.logger.info(
                    "UPDATER | staged %s from %s in %.3fs",
                    remote_version,
                    source,
                    time.perf_counter() - download_started,
                )
                return UpdateResult(
                    current_version=self.current_version,
                    remote_version=remote_version,
                    update_available=True,
                    staged=True,
                )
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.warning(
                    "UPDATER | check=%s failed: %s",
                    source,
                    exc,
                )
                return UpdateResult(
                    current_version=self.current_version,
                    remote_version=self.remote_version,
                    update_available=False,
                    staged=False,
                    error=str(exc),
                )
            finally:
                self.logger.info(
                    "TIMING | update check %s: %.3fs",
                    source,
                    time.perf_counter() - started,
                )

    def request_manual_update(self) -> UpdateResult:
        return self.check_and_stage(
            source="voice",
            request_manual_apply=True,
        )

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "current_version": self.current_version,
                "remote_version": self.remote_version,
                "staged_version": self._staged_version,
                "ready_to_apply": bool(
                    self._auto_apply_ready or self._manual_apply_ready
                ),
                "last_error": self.last_error,
            }

    def auto_apply_ready(self) -> bool:
        with self._state_lock:
            return bool(
                self._auto_apply_ready
                and self._staged_source
                and self._staged_source.exists()
                and not self._apply_launched
            )

    def manual_apply_ready(self) -> bool:
        with self._state_lock:
            return bool(
                self._manual_apply_ready
                and self._staged_source
                and self._staged_source.exists()
                and not self._apply_launched
            )

    def launch_apply(self, *, reason: str) -> bool:
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

        helper_source = APP_DIR / "jarvis_app" / "apply_update.py"
        helper_copy = stage_root / "apply_update_runner.py"
        shutil.copy2(helper_source, helper_copy)

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
        self.logger.info(
            "UPDATER | apply launched | target=%s | reason=%s",
            target_version,
            reason,
        )
        return True
