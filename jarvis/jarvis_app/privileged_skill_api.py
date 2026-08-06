from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import requests

from .paths import DATA_DIR, LOG_DIR


PERMISSION_SPECS: dict[str, dict[str, str]] = {
    "model": {
        "label": "AI model",
        "risk": "Sends the skill input and generated context to the configured OpenAI model.",
    },
    "web_search": {
        "label": "Web search",
        "risk": "Can search the public web and send search queries to OpenAI.",
    },
    "http_requests": {
        "label": "Direct internet requests",
        "risk": "Can contact arbitrary websites and APIs, upload data, and download content.",
    },
    "camera": {
        "label": "Camera",
        "risk": "Can capture images from connected cameras without asking again while the skill remains approved.",
    },
    "screen_capture": {
        "label": "Screen capture",
        "risk": "Can capture visible screen contents, including private messages or passwords shown on screen.",
    },
    "filesystem_read": {
        "label": "Read files",
        "risk": "Can read files that the current Windows account can access.",
    },
    "filesystem_write": {
        "label": "Write files",
        "risk": "Can create and modify files that the current Windows account can access.",
    },
    "filesystem_delete": {
        "label": "Delete files",
        "risk": "Can permanently delete files or folders that the current Windows account can access.",
    },
    "process_execute": {
        "label": "Run programs and commands",
        "risk": "Can launch programs and execute commands with the same Windows permissions as Jarvis.",
    },
    "keyboard_mouse": {
        "label": "Keyboard and mouse control",
        "risk": "Can click, type, press keys, and control applications currently open on the PC.",
    },
    "clipboard": {
        "label": "Clipboard",
        "risk": "Can read or replace clipboard contents, which may include sensitive information.",
    },
    "environment_read": {
        "label": "Environment and credentials",
        "risk": "Can read environment variables, including configured API keys or tokens.",
    },
    "system_info": {
        "label": "System information",
        "risk": "Can inspect running processes, disks, memory, network interfaces, and device details.",
    },
    "open_paths": {
        "label": "Open files, folders, and URLs",
        "risk": "Can open files, folders, applications, and URLs using Windows.",
    },
}

ALL_PERMISSIONS = tuple(PERMISSION_SPECS)
BASIC_PERMISSIONS = {"model", "web_search"}
_AUDIT_FILE = LOG_DIR / "skill_permissions.jsonl"
_POWER_COMMAND_RE = re.compile(
    r"(?i)\b(?:shutdown(?:\.exe)?|restart-computer|stop-computer|"
    r"poweroff|reboot|logoff|rundll32\s+powrprof|init\s+[06])\b"
)


def permission_risks(permissions: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    ordered = [name for name in ALL_PERMISSIONS if name in set(permissions)]
    risks = [PERMISSION_SPECS[name]["risk"] for name in ordered]

    local_data = {
        "camera",
        "screen_capture",
        "filesystem_read",
        "clipboard",
        "environment_read",
        "system_info",
    }
    if set(permissions) & {"http_requests", "web_search"} and set(permissions) & local_data:
        risks.append(
            "Because this skill has both internet and local-data access, it could send "
            "captured or local information to an external service."
        )
    if set(permissions) & {
        "filesystem_write",
        "filesystem_delete",
        "process_execute",
        "keyboard_mouse",
    }:
        risks.append(
            "This skill can change the PC or its data. A bug or poor instruction could "
            "modify files, operate the wrong application, or interrupt other work."
        )
    return list(dict.fromkeys(risks))


def permission_labels(permissions: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    return [
        PERMISSION_SPECS[name]["label"]
        for name in ALL_PERMISSIONS
        if name in set(permissions)
    ]


class SkillPermissionError(PermissionError):
    pass


class SkillAPI:
    """Permission-checked capabilities available to generated Python skills.

    Approval is persisted in the skill manifest. Runtime operations do not prompt
    again, but every privileged call is written to a local audit log.
    """

    def __init__(
        self,
        *,
        skill_id: str,
        permissions: set[str],
        skill_dir: Path,
        test_mode: bool = False,
    ) -> None:
        unknown = permissions - set(ALL_PERMISSIONS)
        if unknown:
            raise SkillPermissionError(f"Unknown permissions: {sorted(unknown)}")
        self.skill_id = skill_id
        self.permissions = set(permissions)
        self.skill_dir = skill_dir.resolve()
        self.test_mode = bool(test_mode)
        self.data_dir = DATA_DIR / "skill_data" / skill_id
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise SkillPermissionError(
                f"Skill '{self.skill_id}' does not have permission: {permission}"
            )

    def _audit(self, action: str, details: dict[str, Any] | None = None) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "skill_id": self.skill_id,
            "action": action,
            "test_mode": self.test_mode,
            "details": details or {},
        }
        with _AUDIT_FILE.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _default_path(self, category: str, suffix: str) -> Path:
        target = self.data_dir / category
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return target / f"{stamp}{suffix}"

    @staticmethod
    def _resolve_path(path: str | os.PathLike[str]) -> Path:
        return Path(path).expanduser().resolve()

    @staticmethod
    def _serializable_headers(headers: Any) -> dict[str, str]:
        return {str(key): str(value) for key, value in dict(headers or {}).items()}

    def model_text(
        self,
        prompt: str,
        image_paths: list[str] | None = None,
        max_output_tokens: int = 1200,
    ) -> dict[str, Any]:
        self._require("model")
        from openai import OpenAI

        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": str(prompt)}
        ]
        for value in image_paths or []:
            path = self._resolve_path(value)
            if self.data_dir not in path.parents and path != self.data_dir:
                self._require("filesystem_read")
            raw = path.read_bytes()
            suffix = path.suffix.lower()
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix, "application/octet-stream")
            content.append(
                {
                    "type": "input_image",
                    "image_url": (
                        f"data:{media_type};base64,"
                        + base64.b64encode(raw).decode("ascii")
                    ),
                }
            )
        model_name = os.getenv("SKILL_RUNTIME_MODEL", "gpt-5.6-luna")
        self._audit(
            "model_text",
            {
                "model": model_name,
                "prompt_chars": len(str(prompt)),
                "image_count": len(image_paths or []),
            },
        )
        response = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "")).responses.create(
            model=model_name,
            input=[{"role": "user", "content": content}],
            max_output_tokens=max(64, min(int(max_output_tokens), 8000)),
            store=False,
        )
        return {
            "model": getattr(response, "model", model_name),
            "text": (response.output_text or "").strip(),
        }

    def web_search(
        self,
        query: str,
        max_output_tokens: int = 1200,
    ) -> dict[str, Any]:
        self._require("web_search")
        from openai import OpenAI

        model_name = os.getenv("SKILL_RUNTIME_MODEL", "gpt-5.6-luna")
        self._audit(
            "web_search",
            {"model": model_name, "query": str(query)},
        )
        response = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "")).responses.create(
            model=model_name,
            input=str(query),
            tools=[{"type": "web_search"}],
            max_output_tokens=max(64, min(int(max_output_tokens), 8000)),
            store=False,
        )
        return {
            "model": getattr(response, "model", model_name),
            "text": (response.output_text or "").strip(),
        }

    def http_request(
        self,
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        timeout: float = 20.0,
        allow_redirects: bool = True,
        max_response_bytes: int = 5_000_000,
    ) -> dict[str, Any]:
        self._require("http_requests")
        method_name = str(method).strip().upper()
        if method_name not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if not re.match(r"^https?://", str(url), re.I):
            raise ValueError("Only http:// and https:// URLs are supported.")
        timeout = max(1.0, min(float(timeout), 120.0))
        maximum = max(1_024, min(int(max_response_bytes), 25_000_000))
        self._audit(
            "http_request",
            {"method": method_name, "url": str(url), "max_response_bytes": maximum},
        )
        response = requests.request(
            method_name,
            str(url),
            headers=self._serializable_headers(headers),
            json=json_body,
            data=data,
            timeout=timeout,
            allow_redirects=bool(allow_redirects),
            stream=True,
        )
        body = bytearray()
        for chunk in response.iter_content(65_536):
            if not chunk:
                continue
            remaining = maximum - len(body)
            if remaining <= 0:
                break
            body.extend(chunk[:remaining])
        content_type = response.headers.get("content-type", "")
        text: str | None = None
        encoded: str | None = None
        if (
            "text/" in content_type.lower()
            or "json" in content_type.lower()
            or "xml" in content_type.lower()
        ):
            text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        else:
            encoded = base64.b64encode(bytes(body)).decode("ascii")
        return {
            "status_code": response.status_code,
            "url": response.url,
            "headers": dict(response.headers),
            "text": text,
            "base64": encoded,
            "truncated": len(body) >= maximum,
        }

    def capture_screenshot(
        self,
        path: str | None = None,
        monitor: int = 1,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        self._require("screen_capture")
        from mss import mss
        from mss.tools import to_png

        destination = (
            self._resolve_path(path)
            if path
            else self._default_path("screenshots", ".png")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with mss() as capture:
            if region:
                box = {
                    "left": int(region["left"]),
                    "top": int(region["top"]),
                    "width": max(1, int(region["width"])),
                    "height": max(1, int(region["height"])),
                }
            else:
                index = max(0, min(int(monitor), len(capture.monitors) - 1))
                box = capture.monitors[index]
            image = capture.grab(box)
            to_png(image.rgb, image.size, output=str(destination))
        self._audit(
            "capture_screenshot",
            {"path": str(destination), "width": image.width, "height": image.height},
        )
        return {
            "path": str(destination),
            "width": image.width,
            "height": image.height,
        }

    def capture_camera(
        self,
        camera_index: int = 0,
        path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        warmup_frames: int = 5,
    ) -> dict[str, Any]:
        self._require("camera")
        import cv2

        destination = (
            self._resolve_path(path)
            if path
            else self._default_path("camera", ".jpg")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        capture = cv2.VideoCapture(int(camera_index), cv2.CAP_DSHOW if os.name == "nt" else 0)
        try:
            if width:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            if height:
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            if not capture.isOpened():
                raise RuntimeError(f"Could not open camera {camera_index}.")
            frame = None
            for _ in range(max(1, min(int(warmup_frames), 30))):
                ok, candidate = capture.read()
                if ok:
                    frame = candidate
            if frame is None:
                raise RuntimeError("The camera returned no frame.")
            if not cv2.imwrite(str(destination), frame):
                raise RuntimeError("Could not save the camera image.")
            actual_height, actual_width = frame.shape[:2]
        finally:
            capture.release()
        self._audit(
            "capture_camera",
            {
                "camera_index": int(camera_index),
                "path": str(destination),
                "width": int(actual_width),
                "height": int(actual_height),
            },
        )
        return {
            "path": str(destination),
            "camera_index": int(camera_index),
            "width": int(actual_width),
            "height": int(actual_height),
        }

    def read_text(
        self,
        path: str,
        encoding: str = "utf-8",
        max_bytes: int = 2_000_000,
    ) -> dict[str, Any]:
        self._require("filesystem_read")
        target = self._resolve_path(path)
        maximum = max(1_024, min(int(max_bytes), 20_000_000))
        raw = target.read_bytes()
        truncated = len(raw) > maximum
        text = raw[:maximum].decode(encoding, errors="replace")
        self._audit("read_text", {"path": str(target), "bytes": min(len(raw), maximum)})
        return {"path": str(target), "text": text, "truncated": truncated}

    def read_bytes_base64(self, path: str, max_bytes: int = 5_000_000) -> dict[str, Any]:
        self._require("filesystem_read")
        target = self._resolve_path(path)
        maximum = max(1_024, min(int(max_bytes), 25_000_000))
        raw = target.read_bytes()
        self._audit("read_bytes", {"path": str(target), "bytes": min(len(raw), maximum)})
        return {
            "path": str(target),
            "base64": base64.b64encode(raw[:maximum]).decode("ascii"),
            "truncated": len(raw) > maximum,
        }

    def list_files(
        self,
        path: str,
        pattern: str = "*",
        recursive: bool = False,
        limit: int = 500,
    ) -> dict[str, Any]:
        self._require("filesystem_read")
        root = self._resolve_path(path)
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        items: list[dict[str, Any]] = []
        for item in iterator:
            try:
                stat = item.stat()
                items.append(
                    {
                        "path": str(item),
                        "name": item.name,
                        "is_file": item.is_file(),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
            except OSError:
                continue
            if len(items) >= max(1, min(int(limit), 5000)):
                break
        self._audit("list_files", {"path": str(root), "count": len(items)})
        return {"path": str(root), "items": items}

    def write_text(
        self,
        path: str,
        text: str,
        append: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        self._require("filesystem_write")
        target = self._resolve_path(path)
        if self.test_mode and self.data_dir not in target.parents and target != self.data_dir:
            self._audit("write_text_simulated", {"path": str(target), "chars": len(text)})
            return {"path": str(target), "written": False, "simulated": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding=encoding, newline="") as handle:
            handle.write(str(text))
        self._audit("write_text", {"path": str(target), "chars": len(str(text))})
        return {"path": str(target), "written": True, "append": bool(append)}

    def copy_path(self, source: str, destination: str) -> dict[str, Any]:
        self._require("filesystem_read")
        self._require("filesystem_write")
        src = self._resolve_path(source)
        dst = self._resolve_path(destination)
        if self.test_mode and self.data_dir not in dst.parents and dst != self.data_dir:
            self._audit("copy_path_simulated", {"source": str(src), "destination": str(dst)})
            return {"copied": False, "simulated": True}
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        self._audit("copy_path", {"source": str(src), "destination": str(dst)})
        return {"copied": True, "source": str(src), "destination": str(dst)}

    def delete_path(self, path: str, recursive: bool = False) -> dict[str, Any]:
        self._require("filesystem_delete")
        target = self._resolve_path(path)
        if self.test_mode:
            self._audit("delete_path_simulated", {"path": str(target)})
            return {"deleted": False, "simulated": True, "path": str(target)}
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        self._audit("delete_path", {"path": str(target), "recursive": bool(recursive)})
        return {"deleted": True, "path": str(target)}

    def run_process(
        self,
        program: str,
        arguments: list[Any] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
        capture_output: bool = True,
    ) -> dict[str, Any]:
        self._require("process_execute")
        command = [str(program), *[str(value) for value in (arguments or [])]]
        joined = " ".join(command)
        if _POWER_COMMAND_RE.search(joined):
            raise SkillPermissionError(
                "PC shutdown and restart are reserved for Jarvis's double-confirmed "
                "pc_power_control tool."
            )
        if self.test_mode:
            self._audit("run_process_simulated", {"command": command})
            return {"started": False, "simulated": True, "command": command}
        started = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=str(self._resolve_path(cwd)) if cwd else None,
            capture_output=bool(capture_output),
            text=True,
            timeout=max(1.0, min(float(timeout), 600.0)),
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt" and capture_output
                else 0
            ),
        )
        self._audit(
            "run_process",
            {"command": command, "returncode": result.returncode},
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[-100_000:],
            "stderr": (result.stderr or "")[-100_000:],
            "seconds": round(time.perf_counter() - started, 3),
        }

    def run_shell(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self._require("process_execute")
        if _POWER_COMMAND_RE.search(str(command)):
            raise SkillPermissionError(
                "PC shutdown and restart are reserved for Jarvis's double-confirmed "
                "pc_power_control tool."
            )
        if self.test_mode:
            self._audit("run_shell_simulated", {"command": str(command)})
            return {"started": False, "simulated": True, "command": str(command)}
        result = subprocess.run(
            str(command),
            cwd=str(self._resolve_path(cwd)) if cwd else None,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(1.0, min(float(timeout), 600.0)),
            check=False,
        )
        self._audit(
            "run_shell",
            {"command": str(command), "returncode": result.returncode},
        )
        return {
            "command": str(command),
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[-100_000:],
            "stderr": (result.stderr or "")[-100_000:],
        }

    def open_path(self, target: str) -> dict[str, Any]:
        self._require("open_paths")
        value = str(target)
        if self.test_mode:
            self._audit("open_path_simulated", {"target": value})
            return {"opened": False, "simulated": True, "target": value}
        if os.name == "nt":
            os.startfile(value)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", value])
        else:
            subprocess.Popen(["xdg-open", value])
        self._audit("open_path", {"target": value})
        return {"opened": True, "target": value}

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
        self._require("keyboard_mouse")
        if self.test_mode:
            self._audit("mouse_move_simulated", {"x": x, "y": y})
            return {"moved": False, "simulated": True}
        import pyautogui

        pyautogui.moveTo(int(x), int(y), duration=max(0.0, min(float(duration), 10.0)))
        self._audit("mouse_move", {"x": int(x), "y": int(y)})
        return {"moved": True, "x": int(x), "y": int(y)}

    def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
    ) -> dict[str, Any]:
        self._require("keyboard_mouse")
        if self.test_mode:
            self._audit("mouse_click_simulated", {"x": x, "y": y, "button": button})
            return {"clicked": False, "simulated": True}
        import pyautogui

        pyautogui.click(
            x=None if x is None else int(x),
            y=None if y is None else int(y),
            clicks=max(1, min(int(clicks), 20)),
            interval=max(0.0, min(float(interval), 5.0)),
            button=str(button),
        )
        self._audit(
            "mouse_click",
            {"x": x, "y": y, "button": button, "clicks": int(clicks)},
        )
        return {"clicked": True}

    def press_key(self, key: str, presses: int = 1, interval: float = 0.0) -> dict[str, Any]:
        self._require("keyboard_mouse")
        if self.test_mode:
            self._audit("press_key_simulated", {"key": key, "presses": presses})
            return {"pressed": False, "simulated": True}
        import pyautogui

        pyautogui.press(
            str(key),
            presses=max(1, min(int(presses), 100)),
            interval=max(0.0, min(float(interval), 2.0)),
        )
        self._audit("press_key", {"key": str(key), "presses": int(presses)})
        return {"pressed": True}

    def hotkey(self, keys: list[str]) -> dict[str, Any]:
        self._require("keyboard_mouse")
        normalized = [str(key) for key in keys]
        if self.test_mode:
            self._audit("hotkey_simulated", {"keys": normalized})
            return {"pressed": False, "simulated": True}
        import pyautogui

        pyautogui.hotkey(*normalized)
        self._audit("hotkey", {"keys": normalized})
        return {"pressed": True, "keys": normalized}

    def type_text(self, text: str, interval: float = 0.0) -> dict[str, Any]:
        self._require("keyboard_mouse")
        if self.test_mode:
            self._audit("type_text_simulated", {"chars": len(str(text))})
            return {"typed": False, "simulated": True}
        import pyautogui

        pyautogui.write(str(text), interval=max(0.0, min(float(interval), 1.0)))
        self._audit("type_text", {"chars": len(str(text))})
        return {"typed": True, "chars": len(str(text))}

    def clipboard_get(self) -> dict[str, Any]:
        self._require("clipboard")
        import pyperclip

        value = pyperclip.paste()
        self._audit("clipboard_get", {"chars": len(str(value))})
        return {"text": str(value)}

    def clipboard_set(self, text: str) -> dict[str, Any]:
        self._require("clipboard")
        if self.test_mode:
            self._audit("clipboard_set_simulated", {"chars": len(str(text))})
            return {"set": False, "simulated": True}
        import pyperclip

        pyperclip.copy(str(text))
        self._audit("clipboard_set", {"chars": len(str(text))})
        return {"set": True}

    def get_environment(self, name: str) -> dict[str, Any]:
        self._require("environment_read")
        key = str(name)
        value = os.getenv(key)
        self._audit("get_environment", {"name": key, "configured": value is not None})
        return {"name": key, "value": value, "configured": value is not None}

    def list_environment_names(self) -> dict[str, Any]:
        self._require("environment_read")
        names = sorted(os.environ)
        self._audit("list_environment_names", {"count": len(names)})
        return {"names": names}

    def system_info(self) -> dict[str, Any]:
        self._require("system_info")
        import psutil

        disks = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except OSError:
                continue
            disks.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "filesystem": partition.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                }
            )
        payload = {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory": dict(psutil.virtual_memory()._asdict()),
            "boot_time": datetime.fromtimestamp(
                psutil.boot_time(), timezone.utc
            ).isoformat(),
            "disks": disks,
        }
        self._audit("system_info")
        return payload
