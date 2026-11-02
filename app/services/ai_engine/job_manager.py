import json
import logging
import queue
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from app.services.database import Database


logger = logging.getLogger(__name__)


class JobQueueFullError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_job_row(row) -> dict | None:
    if row is None:
        return None
    record = dict(row)
    record["config"] = json.loads(record.pop("config_json"))
    raw_result = record.pop("result_json")
    record["result"] = json.loads(raw_result) if raw_result else None
    return record


class JobStore:
    def __init__(self, database: Database | str | Path):
        self.database = database if isinstance(database, Database) else Database(database)
        self.engine = self.database.engine

    def create(
        self,
        job_id: str,
        config: dict,
        total_trials: int,
        user_id: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO optimization_jobs
                        (job_id, user_id, status, created_at, updated_at,
                         completed_trials, total_trials, config_json)
                    VALUES
                        (:job_id, :user_id, 'queued', :now, :now,
                         0, :total_trials, :config_json)
                    """
                ),
                {
                    "job_id": job_id,
                    "user_id": (
                        user_id if user_id is not None else config.get("user_id")
                    ),
                    "now": now,
                    "total_trials": total_trials,
                    "config_json": json.dumps(config),
                },
            )

    def get(self, job_id: str, user_id: str | None = None) -> dict | None:
        statement = "SELECT * FROM optimization_jobs WHERE job_id = :job_id"
        parameters = {"job_id": job_id}
        if user_id is not None:
            statement += " AND user_id = :user_id"
            parameters["user_id"] = user_id
        with self.engine.connect() as connection:
            row = connection.execute(
                text(statement), parameters
            ).mappings().first()
        return _decode_job_row(row)

    def latest(self, user_id: str | None = None) -> dict | None:
        statement = "SELECT * FROM optimization_jobs"
        parameters = {}
        if user_id is not None:
            statement += " WHERE user_id = :user_id"
            parameters["user_id"] = user_id
        statement += " ORDER BY updated_at DESC, job_id DESC LIMIT 1"
        with self.engine.connect() as connection:
            row = connection.execute(
                text(statement), parameters
            ).mappings().first()
        return _decode_job_row(row)

    def owns_result_file(
        self, user_id: str, filename: str, *, is_report: bool = False
    ) -> bool:
        with self.engine.connect() as connection:
            results = connection.execute(
                text(
                    "SELECT result_json FROM optimization_jobs "
                    "WHERE user_id = :user_id AND status = 'completed' "
                    "AND result_json IS NOT NULL"
                ),
                {"user_id": user_id},
            ).scalars()
            for raw_result in results:
                result = json.loads(raw_result)
                if is_report:
                    if any(
                        report.get("filename") == filename
                        for report in result.get("excel_reports", [])
                    ):
                        return True
                elif result.get("best_set_file") == filename:
                    return True
        return False

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
        fields = ["updated_at = :updated_at"]
        values: dict[str, object] = {"updated_at": _utc_now(), "job_id": job_id}
        for column, value in (
            ("status", status),
            ("completed_trials", completed_trials),
            ("best_value", best_value),
            ("error", error),
        ):
            if value is not None:
                fields.append(f"{column} = :{column}")
                values[column] = value
        if result is not None:
            fields.append("result_json = :result_json")
            values["result_json"] = json.dumps(result)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE optimization_jobs SET {', '.join(fields)} "
                    "WHERE job_id = :job_id"
                ),
                values,
            )

    def queued_ids(self) -> list[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT job_id FROM optimization_jobs "
                    "WHERE status = 'queued' ORDER BY created_at, job_id"
                )
            ).mappings().all()
        return [row["job_id"] for row in rows]

    def queued_count(self) -> int:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT COUNT(*) AS count FROM optimization_jobs "
                    "WHERE status = 'queued'"
                )
            ).mappings().first()
        return row["count"]

    def queue_snapshot(self) -> tuple[list[str], int]:
        with self.engine.connect() as connection:
            queued_rows = connection.execute(
                text(
                    "SELECT job_id FROM optimization_jobs "
                    "WHERE status = 'queued' ORDER BY created_at, job_id"
                )
            ).mappings().all()
            running_row = connection.execute(
                text(
                    "SELECT COUNT(*) AS count FROM optimization_jobs "
                    "WHERE status = 'running'"
                )
            ).mappings().first()
        return [row["job_id"] for row in queued_rows], running_row["count"]

    def fail_interrupted_jobs(self) -> list[str]:
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT job_id FROM optimization_jobs WHERE status = 'running'"
                )
            ).mappings().all()
            connection.execute(
                text(
                    """
                    UPDATE optimization_jobs
                    SET status = 'failed',
                        error = 'Application restarted while this job was running.',
                        updated_at = :updated_at
                    WHERE status = 'running'
                    """
                ),
                {"updated_at": _utc_now()},
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

    def submit(
        self,
        job_id: str,
        config: dict,
        total_trials: int,
        user_id: str | None = None,
    ) -> None:
        with self._submit_lock:
            if self.store.queued_count() >= self.maximum_queued_jobs:
                raise JobQueueFullError(
                    "The optimization queue is full. Wait for a queued job to start."
                )
            self.store.create(job_id, config, total_trials, user_id)
            self._queue.put(job_id)

    def get(self, job_id: str, user_id: str | None = None) -> dict | None:
        return self.store.get(job_id, user_id)

    def latest(self, user_id: str | None = None) -> dict | None:
        return self.store.latest(user_id)

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
