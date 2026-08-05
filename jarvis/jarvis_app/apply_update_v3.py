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


def write_result(
    install: Path,
    *,
    status: str,
    target_version: str,
    message: str,
) -> None:
    result_path = install / "data" / "update_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "status": status,
                "target_version": target_version,
                "message": message,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(result_path)


def process_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_process(pid: int, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return
        time.sleep(0.15)
    raise RuntimeError(
        f"Jarvis PID {pid} did not exit within {int(timeout_seconds)} seconds."
    )


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe update path: {value}")
    return path


def copy_with_retry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _ in range(80):
        try:
            shutil.copy2(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.15)
    if last_error:
        raise last_error


def run_logged(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout: float,
    label: str,
) -> None:
    log(log_path, f"{label}: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        ),
    )
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if output:
        for line in output.splitlines():
            log(log_path, f"{label} | {line}")
    if completed.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {completed.returncode}."
        )


def migrate_env(install: Path, log_path: Path) -> None:
    env_path = install / ".env"
    if not env_path.exists():
        return

    migrations = {
        "TEXT_MODEL": {
            "gpt-4.1-mini": "gpt-5.6-luna",
            "gpt-5.1": "gpt-5.6-luna",
        },
        "FOLLOWUP_MODEL": {
            "gpt-4.1-nano": "gpt-5.4-nano",
        },
        "SKILL_PLANNER_MODEL": {
            "gpt-5.2": "gpt-5.6-sol",
        },
        "SKILL_BUILDER_MODEL": {
            "gpt-5.1": "gpt-5.6-luna",
        },
        "SKILL_RUNTIME_MODEL": {
            "gpt-5.1": "gpt-5.6-luna",
        },
        "LOCAL_TTS_SWEDISH_VOICE": {
            "sv_SE-nst-medium": "sv_SE-lisa-medium",
        },
    }
    defaults = {
        "TTS_BACKEND": "cloud",
        "LOCAL_TTS_SWEDISH_VOICE": "sv_SE-lisa-medium",
        "LOCAL_TTS_KOKORO_VOICE": "bm_george",
        "LOCAL_TTS_KOKORO_LANGUAGE": "en-gb",
        "LOCAL_TTS_KOKORO_SPEED": "1.03",
    }

    text = env_path.read_text(encoding="utf-8-sig")
    output: list[str] = []
    seen: set[str] = set()
    changes: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        name, value = line.split("=", 1)
        key = name.strip()
        current = value.strip()
        seen.add(key)
        replacement = migrations.get(key, {}).get(current)
        if replacement:
            output.append(f"{key}={replacement}")
            changes.append(f"{key}:{current}->{replacement}")
        else:
            output.append(line)

    for key, value in defaults.items():
        if key not in seen:
            output.append(f"{key}={value}")
            changes.append(f"{key}:added")

    temporary = env_path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(env_path)
    if changes:
        log(log_path, "environment migration: " + ", ".join(changes))


def restart_background(install: Path) -> None:
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
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        return

    subprocess.Popen(
        [sys.executable, str(install / "jarvis.py")],
        cwd=str(install),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--install", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--target-version", required=True)
    parser.add_argument(
        "--restart-mode",
        choices=["console", "background"],
        default="background",
    )
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

    try:
        log(log_path, f"v3 waiting for Jarvis PID {args.parent_pid}")
        wait_for_process(args.parent_pid)
        log(log_path, f"applying version {args.target_version}")

        for relative in normal_managed:
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

        python = install / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            raise RuntimeError("Jarvis virtual-environment Python is missing.")

        # Always repair dependencies. Comparing requirements files is not enough:
        # a previous interrupted update may have copied the file without installing it.
        run_logged(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade-strategy",
                "only-if-needed",
                "-r",
                str(install / "requirements.txt"),
            ],
            cwd=install,
            log_path=log_path,
            timeout=900,
            label="pip",
        )

        migrate_env(install, log_path)

        verification = (
            "import openai, openwakeword, numpy, sounddevice, soundfile, "
            "requests, jsonschema, piper; "
            "from jarvis_app.settings import Settings; "
            "s=Settings.load(); "
            "bad={'gpt-4.1-mini','gpt-5.2','gpt-5.1'}; "
            "assert s.text_model not in bad; "
            "assert s.skill_planner_model not in bad; "
            "assert s.skill_builder_model not in bad; "
            "print(s.text_model, s.skill_planner_model, s.skill_builder_model)"
        )
        run_logged(
            [str(python), "-c", verification],
            cwd=install,
            log_path=log_path,
            timeout=60,
            label="verification",
        )

        # Advertise the new version only after files, dependencies, and settings pass.
        for relative in late_managed:
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

        installed_version = json.loads(
            (install / "version.json").read_text(encoding="utf-8")
        ).get("version")
        if str(installed_version) != str(args.target_version):
            raise RuntimeError(
                f"Version verification failed: {installed_version}."
            )

        write_result(
            install,
            status="success",
            target_version=args.target_version,
            message=f"Updated to {args.target_version}; dependencies verified.",
        )
        if args.restart_mode == "background":
            restart_background(install)
            log(log_path, "update completed; restarted in background")
        else:
            log(log_path, "update completed; console launcher will restart")

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
                destination.unlink(missing_ok=True)

            write_result(
                install,
                status="failed",
                target_version=args.target_version,
                message=f"Update failed and was rolled back: {exc}",
            )
            if args.restart_mode == "background":
                restart_background(install)
        except Exception as restore_exc:
            log(log_path, f"rollback/restart failed: {restore_exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
