from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time


def wait_for_process(pid: int, timeout_seconds: float = 45.0) -> None:
    if sys.platform == "win32":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.WaitForSingleObject(
                    handle, int(timeout_seconds * 1000)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--install", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    install = Path(args.install).resolve()
    wait_for_process(args.parent_pid)

    if sys.platform == "win32" and (install / "start_jarvis.vbs").exists():
        subprocess.Popen(
            ["wscript.exe", str(install / "start_jarvis.vbs")],
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
