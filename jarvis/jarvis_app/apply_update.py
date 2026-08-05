from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def log(path: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {message}\n")


def wait_for_process(pid: int, timeout_seconds: float = 45.0) -> None:
    if sys.platform == "win32":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.WaitForSingleObject(
                    handle,
                    int(timeout_seconds * 1000),
                )
                return
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.15)


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe update path: {value}")
    return path


def copy_with_retry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _ in range(40):
        try:
            shutil.copy2(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.15)
    if last_error:
        raise last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--install", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--target-version", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source).resolve()
    install = Path(args.install).resolve()
    stage_root = source.parents[2]
    log_path = install / "logs" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    managed = [safe_relative(str(item)) for item in manifest["managed_files"]]
    copy_if_missing = [
        safe_relative(str(item)) for item in manifest.get("copy_if_missing", [])
    ]

    late_names = {"version.json", "manifest.json"}
    normal_managed = [item for item in managed if item.as_posix() not in late_names]
    late_managed = [item for item in managed if item.as_posix() in late_names]

    backup = stage_root / "backup"
    created_destinations: list[Path] = []
    requirements_changed = False

    try:
        log(log_path, f"waiting for Jarvis PID {args.parent_pid}")
        wait_for_process(args.parent_pid)
        log(log_path, f"applying version {args.target_version}")

        old_requirements = install / "requirements.txt"
        new_requirements = source / "requirements.txt"
        if new_requirements.exists():
            requirements_changed = (
                not old_requirements.exists()
                or old_requirements.read_bytes() != new_requirements.read_bytes()
            )

        for relative in normal_managed + late_managed:
            source_file = source / relative
            destination = install / relative
            if not source_file.is_file():
                raise RuntimeError(f"Missing update file: {relative}")

            if destination.exists():
                backup_file = backup / relative
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_file)
            else:
                created_destinations.append(destination)

            copy_with_retry(source_file, destination)

        for relative in copy_if_missing:
            source_file = source / relative
            destination = install / relative
            if not destination.exists() and source_file.is_file():
                copy_with_retry(source_file, destination)
                created_destinations.append(destination)

        if requirements_changed:
            python = install / ".venv" / "Scripts" / "python.exe"
            if not python.exists():
                raise RuntimeError("Jarvis virtual-environment Python is missing.")
            result = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(install / "requirements.txt"),
                ],
                cwd=str(install),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32"
                    else 0
                ),
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Dependency update failed with exit code {result.returncode}."
                )

        restart_script = install / "start_jarvis.vbs"
        if sys.platform == "win32" and restart_script.exists():
            subprocess.Popen(
                ["wscript.exe", str(restart_script)],
                cwd=str(install),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                ),
            )
        else:
            subprocess.Popen(
                [sys.executable, str(install / "jarvis.py")],
                cwd=str(install),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        log(log_path, f"update to {args.target_version} completed")
        time.sleep(0.5)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 0
    except Exception as exc:
        log(log_path, f"update failed: {exc!r}; restoring backup")
        try:
            if backup.exists():
                for backup_file in backup.rglob("*"):
                    if backup_file.is_file():
                        relative = backup_file.relative_to(backup)
                        copy_with_retry(backup_file, install / relative)
            for destination in created_destinations:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass

            restart_script = install / "start_jarvis.vbs"
            if sys.platform == "win32" and restart_script.exists():
                subprocess.Popen(
                    ["wscript.exe", str(restart_script)],
                    cwd=str(install),
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | getattr(subprocess, "DETACHED_PROCESS", 0)
                    ),
                )
        except Exception as restore_exc:
            log(log_path, f"rollback/restart failed: {restore_exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
