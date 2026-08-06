from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any
from uuid import uuid4
import zipfile

from .paths import LOG_DIR


_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|token|authorization|password|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_CONVERSATION_ID: ContextVar[str | None] = ContextVar(
    "jarvis_conversation_id", default=None
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).astimezone().strftime("%Y%m%d_%H%M%S")


def redact(value: Any) -> str:
    text = str(value)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return _EMAIL_RE.sub("[REDACTED_EMAIL]", text)


class SessionLogHandler(logging.Handler):
    def __init__(self, manager: "SessionLogManager") -> None:
        super().__init__(logging.DEBUG)
        self.manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.manager.write_record(record)
        except Exception:
            # Logging must never crash the voice assistant.
            pass


class SessionLogManager:
    """Per-process session logs with per-conversation sublogs and exports."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        retention_days: int = 30,
        max_sessions: int = 100,
        include_transcripts: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.retention_days = max(1, int(retention_days))
        self.max_sessions = max(5, int(max_sessions))
        self.include_transcripts = bool(include_transcripts)
        self.root = LOG_DIR / "sessions"
        self.started_at = _now()
        self.run_id = f"{_stamp(self.started_at)}_{uuid4().hex[:8]}"
        self.run_dir = self.root / self.run_id
        self.conversations_dir = self.run_dir / "conversations"
        self._lock = threading.RLock()
        self._conversation_counter = 0
        self._conversation_dirs: dict[str, Path] = {}
        self._closed = False

        if self.enabled:
            self.conversations_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(
                self.run_dir / "metadata.json",
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "started_at": self.started_at.isoformat(),
                    "ended_at": None,
                    "status": "running",
                    "pid": os.getpid(),
                    "privacy": {
                        "transcripts_enabled": self.include_transcripts,
                        "basic_secret_redaction": True,
                    },
                },
            )
            self.cleanup_old_sessions()

    @classmethod
    def from_environment(cls) -> "SessionLogManager":
        return cls(
            enabled=os.getenv("SESSION_LOGGING_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            retention_days=int(os.getenv("SESSION_LOG_RETENTION_DAYS", "30")),
            max_sessions=int(os.getenv("SESSION_LOG_MAX_SESSIONS", "100")),
            include_transcripts=os.getenv(
                "SESSION_LOG_INCLUDE_TRANSCRIPTS", "true"
            ).strip().lower()
            not in {"0", "false", "no", "off"},
        )

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _append_line(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(value.rstrip("\r\n") + "\n")

    def handler(self) -> SessionLogHandler:
        return SessionLogHandler(self)

    @property
    def current_conversation_id(self) -> str | None:
        return _CONVERSATION_ID.get()

    def start_conversation(self, source: str = "voice") -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            self._conversation_counter += 1
            started = _now()
            conversation_id = (
                f"{self._conversation_counter:03d}_{_stamp(started)}_"
                f"{uuid4().hex[:6]}"
            )
            directory = self.conversations_dir / conversation_id
            directory.mkdir(parents=True, exist_ok=True)
            self._conversation_dirs[conversation_id] = directory
            self._write_json(
                directory / "metadata.json",
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "conversation_id": conversation_id,
                    "source": source,
                    "started_at": started.isoformat(),
                    "ended_at": None,
                    "status": "active",
                },
            )
            _CONVERSATION_ID.set(conversation_id)
            self._append_jsonl(
                self.run_dir / "conversation_index.jsonl",
                {
                    "event": "conversation_started",
                    "conversation_id": conversation_id,
                    "source": source,
                    "timestamp": started.isoformat(),
                },
            )
            return conversation_id

    def ensure_conversation(self, source: str = "unknown") -> str | None:
        return self.current_conversation_id or self.start_conversation(source)

    def end_conversation(
        self,
        conversation_id: str | None = None,
        *,
        status: str = "completed",
    ) -> None:
        if not self.enabled:
            return
        target = conversation_id or self.current_conversation_id
        if not target:
            return
        with self._lock:
            directory = self._conversation_dirs.get(
                target, self.conversations_dir / target
            )
            metadata_path = directory / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "conversation_id": target,
                }
            ended = _now()
            metadata["ended_at"] = ended.isoformat()
            metadata["status"] = status
            self._write_json(metadata_path, metadata)
            self._append_jsonl(
                self.run_dir / "conversation_index.jsonl",
                {
                    "event": "conversation_ended",
                    "conversation_id": target,
                    "status": status,
                    "timestamp": ended.isoformat(),
                },
            )
            if self.current_conversation_id == target:
                _CONVERSATION_ID.set(None)

    def record_transcript(
        self,
        role: str,
        text: str,
        *,
        source: str,
        language: str | None = None,
    ) -> None:
        if not self.enabled or not self.include_transcripts or not text:
            return
        conversation_id = self.ensure_conversation(source)
        value = {
            "timestamp": _now().isoformat(),
            "run_id": self.run_id,
            "conversation_id": conversation_id,
            "role": role,
            "source": source,
            "language": language,
            "text": redact(text),
        }
        with self._lock:
            self._append_jsonl(self.run_dir / "transcript.jsonl", value)
            if conversation_id:
                directory = self._conversation_dirs.get(
                    conversation_id,
                    self.conversations_dir / conversation_id,
                )
                self._append_jsonl(directory / "transcript.jsonl", value)

    def _append_jsonl(self, path: Path, value: dict[str, Any]) -> None:
        self._append_line(
            path,
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
        )

    def write_record(self, record: logging.LogRecord) -> None:
        if not self.enabled:
            return
        created = datetime.fromtimestamp(record.created, timezone.utc)
        message = redact(record.getMessage())
        conversation_id = self.current_conversation_id
        readable = (
            f"{created.astimezone():%Y-%m-%d %H:%M:%S} | "
            f"{record.levelname:<8} | {message}"
        )
        event = {
            "timestamp": created.isoformat(),
            "run_id": self.run_id,
            "conversation_id": conversation_id,
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "thread": record.threadName,
            "message": message,
        }
        with self._lock:
            self._append_line(self.run_dir / "session.log", readable)
            self._append_jsonl(self.run_dir / "events.jsonl", event)
            if conversation_id:
                directory = self._conversation_dirs.get(
                    conversation_id,
                    self.conversations_dir / conversation_id,
                )
                self._append_line(directory / "conversation.log", readable)
                self._append_jsonl(directory / "events.jsonl", event)

    def cleanup_old_sessions(self) -> None:
        if not self.enabled or not self.root.exists():
            return
        with self._lock:
            directories = [
                item for item in self.root.iterdir()
                if item.is_dir() and item != self.run_dir
            ]
            directories.sort(
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            cutoff = _now().timestamp() - self.retention_days * 86400
            for index, directory in enumerate(directories):
                if index >= self.max_sessions or directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        result: list[dict[str, Any]] = []
        directories = sorted(
            (item for item in self.root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for directory in directories[: max(1, min(limit, 50))]:
            try:
                metadata = json.loads(
                    (directory / "metadata.json").read_text(encoding="utf-8")
                )
            except Exception:
                metadata = {"run_id": directory.name}
            metadata["path"] = str(directory)
            result.append(metadata)
        return result

    def export_current(self, destination_dir: Path | None = None) -> Path:
        if not self.enabled:
            raise RuntimeError("Session logging is disabled.")
        if destination_dir is None:
            desktop = Path.home() / "Desktop"
            destination_dir = desktop / "Jarvis Session Logs"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"Jarvis_{self.run_id}.zip"
        temporary = destination.with_suffix(".zip.tmp")
        temporary.unlink(missing_ok=True)
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in self.run_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(self.run_dir))
        temporary.replace(destination)
        return destination

    def close(self, status: str = "stopped") -> None:
        if not self.enabled or self._closed:
            return
        with self._lock:
            current = self.current_conversation_id
            if current:
                self.end_conversation(current, status="interrupted")
            metadata_path = self.run_dir / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {"schema_version": 1, "run_id": self.run_id}
            metadata["ended_at"] = _now().isoformat()
            metadata["status"] = status
            self._write_json(metadata_path, metadata)
            self._closed = True
