import unittest
from unittest.mock import patch

from main import OptimizationRateLimiter


class OptimizationRateLimiterTests(unittest.TestCase):
    def test_allows_up_to_limit_per_client(self):
        limiter = OptimizationRateLimiter(limit=2, window_seconds=60)

        self.assertIsNone(limiter.acquire("192.0.2.1"))
        self.assertIsNone(limiter.acquire("192.0.2.1"))
        self.assertEqual(limiter.acquire("192.0.2.1"), 60)

    def test_limits_clients_independently_and_expires_old_requests(self):
        limiter = OptimizationRateLimiter(limit=1, window_seconds=60)
        with patch("main.time.monotonic", side_effect=[100.0, 100.0, 161.0]):
            self.assertIsNone(limiter.acquire("192.0.2.1"))
            self.assertIsNone(limiter.acquire("192.0.2.2"))
            self.assertIsNone(limiter.acquire("192.0.2.1"))


if __name__ == "__main__":
    unittest.main()
