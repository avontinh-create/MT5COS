import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.services.ai_engine.optimizer import AIOptimizer


class FixedTrial:
    number = 0

    def suggest_int(self, name, low, high, step=1):
        return low

    def suggest_float(self, name, low, high, step=None):
        return low


class OptimizerCredentialCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        base_set = root / "base.set"
        ranges_set = root / "ranges.set"
        base_set.write_text("Param=5||1||1||9||N\n", encoding="utf-8")
        ranges_set.write_text("Param=5||1||1||9||Y\n", encoding="utf-8")

        self.password = "test-only-password"
        self.optimizer = AIOptimizer(
            terminal_path="unused-terminal.exe",
            base_workspace=str(root / "workspace"),
            account_info={
                "login": "12345",
                "password": self.password,
                "server": "Test-Server",
            },
        )
        self.optimizer.setup_task(
            str(base_set),
            "TestEA.ex5",
            optimization_set_file_path=str(ranges_set),
        )
        self.ini_path = (
            Path(self.optimizer.builder.temp_configs_dir) / "trial_run_0.ini"
        )

    def test_ini_is_removed_after_successful_backtest(self):
        def run_backtest(ini_path):
            content = Path(ini_path).read_text(encoding="utf-8")
            self.assertIn(f"Password={self.password}", content)
            self.assertIn("ShutdownTerminal=1", content)
            return True

        self.optimizer.runner.run_backtest = Mock(side_effect=run_backtest)
        self.optimizer.parser.parse_html_report = Mock(
            return_value={
                "NetProfit": 10.0,
                "ProfitFactor": 2.0,
                "MaxDrawdown": 5.0,
            }
        )

        score = self.optimizer.objective(
            FixedTrial(), "run", "EURUSD", "H1", "2023.01.01", "2023.12.31"
        )

        self.assertEqual(score, 4.0)
        self.assertFalse(self.ini_path.exists())

    def test_ini_is_removed_when_backtest_fails(self):
        self.optimizer.runner.run_backtest = Mock(return_value=False)

        with self.assertRaisesRegex(RuntimeError, "non-zero status"):
            self.optimizer.objective(
                FixedTrial(), "run", "EURUSD", "H1", "2023.01.01", "2023.12.31"
            )

        self.assertFalse(self.ini_path.exists())

    def test_ini_is_removed_when_report_parsing_fails(self):
        self.optimizer.runner.run_backtest = Mock(return_value=True)
        self.optimizer.parser.parse_html_report = Mock(
            side_effect=ValueError("invalid report")
        )

        with self.assertRaisesRegex(ValueError, "invalid report"):
            self.optimizer.objective(
                FixedTrial(), "run", "EURUSD", "H1", "2023.01.01", "2023.12.31"
            )

        self.assertFalse(self.ini_path.exists())


if __name__ == "__main__":
    unittest.main()
