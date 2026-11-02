import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class OptimizerWebValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.terminal = self.root / "terminal64.exe"
        self.terminal.touch()
        self.jobs_dir = self.root / "jobs"
        self.workspace_dir = self.root / "workspace"
        self.client = TestClient(main.app)
        self.headers = {
            "Authorization": "Basic "
            + base64.b64encode(b"test-user:test-password-for-web").decode("ascii")
        }
        self.environment = patch.dict(
            "os.environ",
            {
                "MT5_WEB_USERNAME": "test-user",
                "MT5_WEB_PASSWORD": "test-password-for-web",
                "MT5_LOGIN": "12345",
                "MT5_PASSWORD": "broker-password",
                "MT5_SERVER": "Test-Server",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_local_preview_can_open_without_basic_auth(self):
        with patch.dict("os.environ", {"MT5_ALLOW_LOCAL_NO_AUTH": "1"}):
            request = SimpleNamespace(
                client=SimpleNamespace(host="127.0.0.1"),
                url=SimpleNamespace(hostname="127.0.0.1"),
            )

            self.assertTrue(main._is_local_preview_request(request))

    def test_local_preview_flag_does_not_bypass_auth_for_non_local_host(self):
        with patch.dict("os.environ", {"MT5_ALLOW_LOCAL_NO_AUTH": "1"}):
            external_request = SimpleNamespace(
                client=SimpleNamespace(host="203.0.113.8"),
                url=SimpleNamespace(hostname="optimizer.example"),
            )
            forwarded_request = SimpleNamespace(
                client=SimpleNamespace(host="127.0.0.1"),
                url=SimpleNamespace(hostname="optimizer.example"),
            )

            self.assertFalse(main._is_local_preview_request(external_request))
            self.assertFalse(main._is_local_preview_request(forwarded_request))

    def _post(self, *, model=0, execution_mode=0, optimization_file=None):
        files = {
            "file": (
                "base.set",
                b"Param=5||1||1||9||N\n",
                "application/octet-stream",
            )
        }
        if optimization_file is not None:
            files["optimization_file"] = (
                "ranges.set",
                optimization_file,
                "application/octet-stream",
            )
        with (
            patch.object(main, "JOBS_DIR", self.jobs_dir),
            patch.object(main, "WORKSPACE_DIR", self.workspace_dir),
            patch.object(main, "_configured_path", return_value=self.terminal),
            patch.object(main, "_validate_expert_file"),
            patch.object(main, "optimization_rate_limiter", main.OptimizationRateLimiter(3, 60)),
            patch.object(main.job_manager, "submit") as submit,
        ):
            response = self.client.post(
                "/optimize",
                headers=self.headers,
                files=files,
                data={
                    "expert_name": "TestEA.ex5",
                    "symbol": "EURUSD",
                    "period": "H1",
                    "n_trials": "10",
                    "from_date": "2026.01.01",
                    "to_date": "2026.10.31",
                    "deposit": "10000",
                    "currency": "USD",
                    "leverage": "100",
                    "model": str(model),
                    "execution_mode": str(execution_mode),
                },
            )
        return response, submit

    def test_missing_optimization_ranges_are_rejected_before_queueing(self):
        response, submit = self._post()

        self.assertEqual(response.status_code, 422)
        self.assertIn("optimization_ranges.set", response.json()["detail"])
        self.assertIn("File tham", response.json()["detail"])
        submit.assert_not_called()
        self.assertFalse(list(self.jobs_dir.iterdir()))

    def test_random_delay_is_rejected_with_open_prices_only(self):
        response, submit = self._post(model=2, execution_mode=1)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Random Delay", response.json()["detail"])
        submit.assert_not_called()

    def test_matching_optimization_ranges_can_be_queued(self):
        response, submit = self._post(
            optimization_file=b"Param=5||1||1||9||Y\n"
        )

        self.assertEqual(response.status_code, 202)
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
