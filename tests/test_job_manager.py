import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.ai_engine.job_manager import JobStore, OptimizationJobManager


class OptimizationJobManagerTests(unittest.TestCase):
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
            manager.stop()
            job = manager.get("interrupted")

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
