from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import traceback
from typing import Any, Callable
from uuid import uuid4

from .paths import TASK_DB_FILE


ACTIVE_STATUSES = {"queued", "running"}
FINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TaskRecord:
    id: str
    kind: str
    title: str
    status: str
    stage: str
    detail: str
    progress: float
    created_at: str
    updated_at: str
    completed_at: str | None
    result: Any
    error: str | None
    metadata: dict[str, Any]

    def as_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "detail": self.detail,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata,
        }
        if include_result:
            data["result"] = self.result
        return data


class TaskReporter:
    def __init__(self, manager: "TaskManager", task_id: str) -> None:
        self.manager = manager
        self.task_id = task_id

    def update(
        self,
        stage: str,
        detail: str = "",
        progress: float | None = None,
    ) -> None:
        self.manager.update(
            self.task_id,
            status="running",
            stage=stage,
            detail=detail,
            progress=progress,
        )

    def event(self, message: str) -> None:
        self.manager.add_event(self.task_id, message)

    def is_cancelled(self) -> bool:
        record = self.manager.get(self.task_id)
        return bool(record and record.status == "cancelled")


TaskWorker = Callable[[TaskReporter], Any]


class TaskManager:
    def __init__(
        self,
        logger: logging.Logger,
        *,
        db_path: Path = TASK_DB_FILE,
        max_workers: int = 3,
    ) -> None:
        self.logger = logger
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="JarvisTask",
        )
        self._futures: dict[str, Any] = {}
        self._init_db()
        self._recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _init_db(self) -> None:
        with self._db_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    progress REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
                    ON tasks(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, id DESC);
                """
            )

    def _recover_interrupted(self) -> None:
        now = utc_now()
        with self._db_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status='interrupted',
                    stage='Interrupted',
                    detail='Jarvis stopped before this background task finished.',
                    updated_at=?,
                    completed_at=?,
                    error=COALESCE(error, 'Jarvis stopped before completion.')
                WHERE status IN ('queued', 'running')
                """,
                (now, now),
            )
            if cursor.rowcount:
                self.logger.warning(
                    "TASKS | marked %d unfinished task(s) as interrupted",
                    cursor.rowcount,
                )

    @staticmethod
    def _decode_json(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def _row_to_record(self, row: sqlite3.Row | None) -> TaskRecord | None:
        if row is None:
            return None
        return TaskRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            status=str(row["status"]),
            stage=str(row["stage"]),
            detail=str(row["detail"]),
            progress=float(row["progress"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] else None
            ),
            result=self._decode_json(row["result_json"], None),
            error=str(row["error"]) if row["error"] else None,
            metadata=self._decode_json(row["metadata_json"], {}),
        )

    def create(
        self,
        kind: str,
        title: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        task_id = uuid4().hex[:12]
        now = utc_now()
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, kind, title, status, stage, detail, progress,
                    created_at, updated_at, completed_at, result_json,
                    error, metadata_json
                ) VALUES (?, ?, ?, 'queued', 'Queued', '', 0.0, ?, ?, NULL,
                          NULL, NULL, ?)
                """,
                (
                    task_id,
                    kind,
                    title,
                    now,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
        record = self.get(task_id)
        assert record is not None
        self.logger.info("TASKS | created %s | %s | %s", task_id, kind, title)
        return record

    def submit(
        self,
        kind: str,
        title: str,
        worker: TaskWorker,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        record = self.create(kind, title, metadata=metadata)
        future = self._executor.submit(self._execute, record.id, worker)
        self._futures[record.id] = future
        return record

    def _execute(self, task_id: str, worker: TaskWorker) -> None:
        reporter = TaskReporter(self, task_id)
        self.update(
            task_id,
            status="running",
            stage="Starting",
            detail="Background work has started.",
            progress=0.01,
        )
        try:
            result = worker(reporter)
        except Exception as exc:
            self.logger.exception("TASKS | task %s failed", task_id)
            self.update(
                task_id,
                status="failed",
                stage="Failed",
                detail=str(exc),
                progress=1.0,
                error=f"{type(exc).__name__}: {exc}",
                result={"traceback": traceback.format_exc()},
                completed=True,
            )
        else:
            current = self.get(task_id)
            if current and current.status == "cancelled":
                return
            self.update(
                task_id,
                status="completed",
                stage="Completed",
                detail="Finished successfully.",
                progress=1.0,
                result=result,
                completed=True,
            )
        finally:
            self._futures.pop(task_id, None)

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        detail: str | None = None,
        progress: float | None = None,
        result: Any = ...,
        error: str | None | object = ...,
        completed: bool = False,
    ) -> None:
        existing = self.get(task_id)
        if existing is None:
            raise KeyError(f"Unknown task: {task_id}")

        new_status = status if status is not None else existing.status
        new_stage = stage if stage is not None else existing.stage
        new_detail = detail if detail is not None else existing.detail
        new_progress = (
            min(1.0, max(0.0, float(progress)))
            if progress is not None
            else existing.progress
        )
        new_result = existing.result if result is ... else result
        new_error = existing.error if error is ... else error
        now = utc_now()
        completed_at = now if completed or new_status in FINAL_STATUSES else None

        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status=?, stage=?, detail=?, progress=?, updated_at=?,
                    completed_at=COALESCE(?, completed_at), result_json=?, error=?
                WHERE id=?
                """,
                (
                    new_status,
                    new_stage,
                    new_detail,
                    new_progress,
                    now,
                    completed_at,
                    (
                        json.dumps(new_result, ensure_ascii=False, default=str)
                        if new_result is not None
                        else None
                    ),
                    new_error,
                    task_id,
                ),
            )
        if detail and detail != existing.detail:
            self.add_event(task_id, f"{new_stage}: {detail}")
        self.logger.info(
            "TASKS | %s | %s | %.0f%% | %s",
            task_id,
            new_stage,
            new_progress * 100,
            new_detail,
        )

    def add_event(self, task_id: str, message: str) -> None:
        with self._db_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO task_events(task_id, created_at, message) VALUES (?, ?, ?)",
                (task_id, utc_now(), message),
            )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        return self._row_to_record(row)

    def latest(self, *, active_first: bool = True) -> TaskRecord | None:
        order = (
            "CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END, "
            if active_first
            else ""
        )
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM tasks ORDER BY {order} updated_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_record(row)

    def recent(self, limit: int = 8) -> list[TaskRecord]:
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()
        return [record for row in rows if (record := self._row_to_record(row))]

    def active(self) -> list[TaskRecord]:
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('queued','running')
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [record for row in rows if (record := self._row_to_record(row))]

    def has_active(self) -> bool:
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
        return row is not None

    def status_payload(
        self,
        task_id: str | None = None,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        record = self.get(task_id) if task_id else self.latest()
        if record is None:
            return {"found": False, "message": "No background tasks exist."}
        return {
            "found": True,
            "task": record.as_dict(include_result=include_result),
        }

    def spoken_status(self, *, swedish: bool = False) -> str:
        active = self.active()
        record = active[0] if active else self.latest(active_first=False)
        if record is None:
            return "Inget körs i bakgrunden." if swedish else "Nothing is running."

        percent = int(round(record.progress * 100))
        if record.status in ACTIVE_STATUSES:
            if swedish:
                detail = record.detail or record.stage
                return f"{record.stage}: {detail} Ungefär {percent} procent klart."
            detail = record.detail or record.stage
            return f"{record.stage}: {detail} About {percent} percent complete."

        if record.status == "completed":
            summary = ""
            if isinstance(record.result, dict):
                summary = str(
                    record.result.get("spoken_summary")
                    or record.result.get("summary")
                    or ""
                ).strip()
            if swedish:
                return summary or f"{record.title} är klart."
            return summary or f"{record.title} is finished."

        if swedish:
            return f"{record.title} misslyckades: {record.error or record.detail}"
        return f"{record.title} failed: {record.error or record.detail}"

    def prompt_context(self) -> str:
        records = self.recent(limit=5)
        if not records:
            return "No background tasks have been created."
        lines = []
        for record in records:
            lines.append(
                f"- {record.id}: {record.title}; status={record.status}; "
                f"stage={record.stage}; progress={int(record.progress * 100)}%; "
                f"detail={record.detail}"
            )
        return "\n".join(lines)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
