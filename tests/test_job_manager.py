import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from app.services.ai_engine.job_manager import (
    JobQueueFullError,
    JobStore,
    OptimizationJobManager,
)


class OptimizationJobManagerTests(unittest.TestCase):
    def test_rejects_jobs_when_queue_is_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = OptimizationJobManager(
                JobStore(root / "jobs.sqlite3"),
                root / "jobs",
                lambda *_: {},
                maximum_queued_jobs=1,
            )
            manager.submit("first", {"job_id": "first"}, 1)

            with self.assertRaises(JobQueueFullError):
                manager.submit("second", {"job_id": "second"}, 1)

            self.assertEqual(manager.store.queued_count(), 1)

    def test_concurrent_claimers_can_only_claim_a_job_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create("one-job", {"job_id": "one-job"}, 1)

            with ThreadPoolExecutor(max_workers=8) as executor:
                claims = list(executor.map(lambda _: store.claim_next_job(), range(8)))

            claimed_jobs = [claim for claim in claims if claim is not None]
            self.assertEqual(len(claimed_jobs), 1)
            self.assertEqual(claimed_jobs[0]["job_id"], "one-job")
            self.assertEqual(store.get("one-job")["status"], "running")

    def test_queue_capacity_is_enforced_across_manager_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager_a = OptimizationJobManager(
                JobStore(root / "jobs.sqlite3"),
                root / "jobs",
                lambda *_: {},
                maximum_queued_jobs=1,
            )
            manager_b = OptimizationJobManager(
                JobStore(root / "jobs.sqlite3"),
                root / "jobs",
                lambda *_: {},
                maximum_queued_jobs=1,
            )

            def submit(manager, job_id):
                try:
                    manager.submit(job_id, {"job_id": job_id}, 1)
                    return "accepted"
                except JobQueueFullError:
                    return "full"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda args: submit(*args),
                        ((manager_a, "first"), (manager_b, "second")),
                    )
                )

            self.assertCountEqual(results, ["accepted", "full"])
            self.assertEqual(manager_a.store.queued_count(), 1)

    def test_queued_job_can_be_processed_by_another_manager_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JobStore(root / "jobs.sqlite3")
            submitted_by = OptimizationJobManager(
                store, root / "jobs", lambda *_: {}
            )
            finished = threading.Event()
            processed = []
            active_workers = 0
            maximum_active_workers = 0
            state_lock = threading.Lock()

            def execute(config, progress):
                nonlocal active_workers, maximum_active_workers
                with state_lock:
                    active_workers += 1
                    maximum_active_workers = max(
                        maximum_active_workers, active_workers
                    )
                time.sleep(0.05)
                with state_lock:
                    processed.append(config["job_id"])
                    active_workers -= 1
                    if len(processed) == 2:
                        finished.set()
                return {"best_value": 1.0}

            serving_managers = [
                OptimizationJobManager(
                    JobStore(root / "jobs.sqlite3"),
                    root / "jobs",
                    execute,
                )
                for _ in range(2)
            ]
            for job_id in ("cross-process-job-1", "cross-process-job-2"):
                submitted_by.submit(job_id, {"job_id": job_id}, 1)

            for manager in serving_managers:
                manager.start()
            try:
                self.assertTrue(
                    finished.wait(5), "The durable queued jobs were not processed."
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    jobs = [
                        manager.get("cross-process-job-1")
                        for manager in serving_managers
                    ]
                    job = jobs[0]
                    second_job = serving_managers[0].get("cross-process-job-2")
                    if (
                        job["status"] == "completed"
                        and second_job["status"] == "completed"
                    ):
                        break
                    time.sleep(0.01)
                self.assertCountEqual(
                    processed, ["cross-process-job-1", "cross-process-job-2"]
                )
                self.assertEqual(maximum_active_workers, 1)
                self.assertEqual(job["status"], "completed")
                self.assertEqual(second_job["status"], "completed")
            finally:
                for manager in serving_managers:
                    manager.stop()

    def test_reports_queue_position_and_running_job_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JobStore(root / "jobs.sqlite3")
            store.create("first", {"job_id": "first"}, 1)
            store.create("second", {"job_id": "second"}, 1)
            store.create("active", {"job_id": "active"}, 1)
            store.update("active", status="running")
            manager = OptimizationJobManager(
                store, root / "jobs", lambda *_: {}, maximum_queued_jobs=5
            )

            self.assertEqual(
                manager.queue_status("second"),
                {
                    "queued_jobs": 2,
                    "running_jobs": 1,
                    "maximum_queued_jobs": 5,
                    "queue_position": 2,
                },
            )
            self.assertIsNone(manager.queue_status("active")["queue_position"])

    def test_reads_latest_job_and_result_from_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create("latest", {"job_id": "latest"}, 1)
            store.update(
                "latest",
                status="completed",
                result={"terminal_build": 5000},
            )

            latest = store.latest()

            self.assertEqual(latest["job_id"], "latest")
            self.assertEqual(latest["result"]["terminal_build"], 5000)

    def test_runs_job_and_persists_progress_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "jobs" / "job-1"
            uploads.mkdir(parents=True)
            (uploads / "parameters.set").write_text("Param=1\n", encoding="utf-8")
            finished = threading.Event()
            store = JobStore(root / "jobs.sqlite3")

            def execute(config, progress):
                progress(1, 2, 1.5)
                progress(2, 2, 2.5)
                return {"best_value": 2.5, "best_params": {"Param": 7}}

            manager = OptimizationJobManager(store, root / "jobs", execute)
            manager.start()
            manager.submit("job-1", {"job_id": "job-1"}, 2)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = manager.get("job-1")
                if job and job["status"] in {"completed", "failed"}:
                    finished.set()
                    break
                time.sleep(0.01)
            manager.stop()

            self.assertTrue(finished.is_set(), "The optimization job did not finish.")
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["completed_trials"], 2)
            self.assertEqual(job["best_value"], 2.5)
            self.assertEqual(job["result"]["best_params"], {"Param": 7})
            self.assertFalse(uploads.exists())
            self.assertEqual(store.get("job-1")["status"], "completed")

    def test_marks_interrupted_jobs_failed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JobStore(root / "jobs.sqlite3")
            store.create("interrupted", {"job_id": "interrupted"}, 5)
            store.update("interrupted", status="running", completed_trials=2)

            manager = OptimizationJobManager(store, root / "jobs", lambda *_: {})
            manager.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = manager.get("interrupted")
                if job["status"] == "failed":
                    break
                time.sleep(0.01)
            manager.stop()

            self.assertEqual(job["status"], "failed")
            self.assertIn("restarted", job["error"])
            self.assertEqual(job["completed_trials"], 2)

    def test_records_job_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JobStore(root / "jobs.sqlite3")
            manager = OptimizationJobManager(
                store, root / "jobs", lambda *_: (_ for _ in ()).throw(
                    RuntimeError("tester failed")
                )
            )
            manager.start()
            manager.submit("failed", {"job_id": "failed"}, 1)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = manager.get("failed")
                if job and job["status"] == "failed":
                    break
                time.sleep(0.01)
            manager.stop()

            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], "tester failed")


if __name__ == "__main__":
    unittest.main()
