import json
import logging
import queue
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)


class JobQueueFullError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_trials INTEGER NOT NULL DEFAULT 0,
                    total_trials INTEGER NOT NULL,
                    best_value REAL,
                    error TEXT,
                    config_json TEXT NOT NULL,
                    result_json TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, job_id: str, config: dict, total_trials: int) -> None:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO optimization_jobs
                    (job_id, status, created_at, updated_at, total_trials, config_json)
                VALUES (?, 'queued', ?, ?, ?, ?)
                """,
                (job_id, now, now, total_trials, json.dumps(config)),
            )

    def get(self, job_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM optimization_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None

        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        raw_result = record.pop("result_json")
        record["result"] = json.loads(raw_result) if raw_result else None
        return record

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        completed_trials: int | None = None,
        best_value: float | None = None,
        error: str | None = None,
        result: dict | None = None,
    ) -> None:
        fields = ["updated_at = ?"]
        values: list[object] = [_utc_now()]
        for column, value in (
            ("status", status),
            ("completed_trials", completed_trials),
            ("best_value", best_value),
            ("error", error),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        if result is not None:
            fields.append("result_json = ?")
            values.append(json.dumps(result))
        values.append(job_id)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE optimization_jobs SET {', '.join(fields)} WHERE job_id = ?",
                values,
            )

    def queued_ids(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM optimization_jobs "
                "WHERE status = 'queued' ORDER BY created_at, rowid"
            ).fetchall()
        return [row["job_id"] for row in rows]

    def queued_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM optimization_jobs "
                "WHERE status = 'queued'"
            ).fetchone()
        return row["count"]

    def queue_snapshot(self) -> tuple[list[str], int]:
        with self._connection() as connection:
            queued_rows = connection.execute(
                "SELECT job_id FROM optimization_jobs "
                "WHERE status = 'queued' ORDER BY created_at, rowid"
            ).fetchall()
            running_row = connection.execute(
                "SELECT COUNT(*) AS count FROM optimization_jobs "
                "WHERE status = 'running'"
            ).fetchone()
        return [row["job_id"] for row in queued_rows], running_row["count"]

    def fail_interrupted_jobs(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM optimization_jobs WHERE status = 'running'"
            ).fetchall()
            connection.execute(
                """
                UPDATE optimization_jobs
                SET status = 'failed',
                    error = 'Application restarted while this job was running.',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (_utc_now(),),
            )
        return [row["job_id"] for row in rows]


class OptimizationJobManager:
    def __init__(
        self,
        store: JobStore,
        jobs_directory: str | Path,
        run_job: Callable[[dict, Callable[[int, int, float | None], None]], dict],
        maximum_queued_jobs: int = 5,
    ):
        if maximum_queued_jobs < 1:
            raise ValueError("maximum_queued_jobs must be greater than zero.")
        self.store = store
        self.jobs_directory = Path(jobs_directory).resolve()
        self.run_job = run_job
        self.maximum_queued_jobs = maximum_queued_jobs
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            for job_id in self.store.fail_interrupted_jobs():
                self._remove_upload_directory(job_id)
            for job_id in self.store.queued_ids():
                self._queue.put(job_id)
            self._thread = threading.Thread(
                target=self._work,
                name="mt5-optimization-jobs",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def submit(self, job_id: str, config: dict, total_trials: int) -> None:
        with self._submit_lock:
            if self.store.queued_count() >= self.maximum_queued_jobs:
                raise JobQueueFullError(
                    "The optimization queue is full. Wait for a queued job to start."
                )
            self.store.create(job_id, config, total_trials)
            self._queue.put(job_id)

    def get(self, job_id: str) -> dict | None:
        return self.store.get(job_id)

    def queue_status(self, job_id: str | None = None) -> dict:
        queued_job_ids, running_jobs = self.store.queue_snapshot()
        return {
            "queued_jobs": len(queued_job_ids),
            "running_jobs": running_jobs,
            "maximum_queued_jobs": self.maximum_queued_jobs,
            "queue_position": (
                queued_job_ids.index(job_id) + 1
                if job_id in queued_job_ids
                else None
            ),
        }

    def _work(self) -> None:
        while True:
            if self._stop_event.is_set():
                return
            job_id = self._queue.get()
            if job_id is None:
                return
            record = self.store.get(job_id)
            if record is None or record["status"] != "queued":
                continue

            self.store.update(job_id, status="running")
            config = record["config"]

            def progress(
                completed_trials: int,
                total_trials: int,
                best_value: float | None,
            ) -> None:
                self.store.update(
                    job_id,
                    completed_trials=completed_trials,
                    best_value=best_value,
                )

            try:
                result = self.run_job(config, progress)
            except Exception as exc:
                logger.exception("Optimization job %s failed", job_id)
                self.store.update(job_id, status="failed", error=str(exc))
            else:
                self.store.update(
                    job_id,
                    status="completed",
                    completed_trials=record["total_trials"],
                    best_value=result.get("best_value"),
                    result=result,
                )
            finally:
                self._remove_upload_directory(job_id)

    def _remove_upload_directory(self, job_id: str) -> None:
        upload_directory = (self.jobs_directory / job_id).resolve()
        if upload_directory.parent != self.jobs_directory:
            raise ValueError("Optimization job upload path escaped its directory.")
        if upload_directory.exists():
            try:
                shutil.rmtree(upload_directory)
            except OSError:
                logger.exception("Could not remove uploads for job %s", job_id)
