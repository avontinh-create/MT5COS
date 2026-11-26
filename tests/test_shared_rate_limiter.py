import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.database import Database
from app.services.rate_limiter import SharedRateLimiter


class SharedRateLimiterTests(unittest.TestCase):
    def test_limit_is_shared_across_limiter_instances_and_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "rate-limit.sqlite3")
            limiters = [
                SharedRateLimiter(
                    database.engine,
                    scope="login",
                    limit=5,
                    window_seconds=900,
                )
                for _ in range(20)
            ]
            barrier = threading.Barrier(len(limiters))
            results = []
            result_lock = threading.Lock()

            def submit(limiter):
                barrier.wait()
                result = limiter.acquire("198.51.100.10")
                with result_lock:
                    results.append(result)

            threads = [
                threading.Thread(target=submit, args=(limiter,))
                for limiter in limiters
            ]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(results.count(None), 5)
                self.assertEqual(len([value for value in results if value]), 15)
            finally:
                database.engine.dispose()

    def test_limit_window_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "rate-limit-expiry.sqlite3")
            limiter = SharedRateLimiter(
                database.engine,
                scope="signup",
                limit=1,
                window_seconds=60,
            )
            try:
                with patch("app.services.rate_limiter.time.time", return_value=1000):
                    self.assertIsNone(limiter.acquire("198.51.100.10"))
                with patch("app.services.rate_limiter.time.time", return_value=1030):
                    self.assertEqual(limiter.acquire("198.51.100.10"), 30)
                with patch(
                    "app.services.rate_limiter.time.time", return_value=1061
                ):
                    self.assertIsNone(limiter.acquire("198.51.100.10"))
            finally:
                database.engine.dispose()


if __name__ == "__main__":
    unittest.main()
