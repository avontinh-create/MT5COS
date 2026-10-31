import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.mt5_engine.builder import MT5ConfigBuilder
from app.services.mt5_engine.parser import ReportParser
from app.services.mt5_engine.runner import MT5BacktestRunner


class MT5BacktestRunnerTests(unittest.TestCase):
    def test_connection_check_reports_broker_without_exposing_login(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = Path(directory) / "terminal64.exe"
            terminal.touch()
            mt5 = Mock()
            mt5.initialize.return_value = True
            mt5.account_info.return_value = SimpleNamespace(
                login=1234,
                server="Broker-Demo",
                currency="USD",
            )
            mt5.terminal_info.return_value = SimpleNamespace(
                connected=True,
                build=5000,
            )

            with (
                patch.dict(sys.modules, {"MetaTrader5": mt5}),
                patch.object(MT5BacktestRunner, "_terminal_process_ids", return_value=set()),
            ):
                result = MT5BacktestRunner(str(terminal)).check_connection(
                    "1234", "secret", "Broker-Demo"
                )

            self.assertEqual(
                result,
                {
                    "connected": True,
                    "server": "Broker-Demo",
                    "currency": "USD",
                    "terminal_build": 5000,
                },
            )
            self.assertNotIn("login", result)
            mt5.shutdown.assert_called_once()

    def test_connection_check_rejects_an_existing_terminal_session(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = Path(directory) / "terminal64.exe"
            terminal.touch()
            runner = MT5BacktestRunner(str(terminal))
            with patch.object(
                MT5BacktestRunner, "_terminal_process_ids", return_value={42}
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Close the portable MT5 terminal"
                ):
                    runner.check_connection("1234", "secret", "Broker-Demo")

    def test_parses_actual_mt5_report_summary_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.htm"
            report.write_text(
                "<table>"
                "<tr><td>Total Net Profit:</td><td>100.00</td></tr>"
                "<tr><td>Balance Drawdown Maximal:</td><td>20.00 (10.00%)</td></tr>"
                "<tr><td>Equity Drawdown Maximal:</td><td>30.00 (15.00%)</td></tr>"
                "<tr><td>Profit Factor:</td><td>1.50</td></tr>"
                "</table>",
                encoding="utf-8",
            )

            metrics = ReportParser().parse_html_report(str(report))

            self.assertEqual(
                metrics,
                {"NetProfit": 100.0, "MaxDrawdown": 15.0, "ProfitFactor": 1.5},
            )

    def test_backtest_ini_uses_expert_parameters_set_file_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_file = root / "trial.set"
            report_file = root / "report.htm"
            builder = MT5ConfigBuilder(str(root / "workspace"))

            ini_path = builder.build_ini_file(
                trial_id="test",
                expert_name="Peak.ex5",
                symbol="EURUSDm",
                period="H1",
                deposit=25000,
                currency="EUR",
                leverage=200,
                model=0,
                execution_mode=1,
                set_file_path=str(set_file),
                report_path=str(report_file),
                login="1234",
                password="test-secret",
                server="Broker-Demo",
            )
            content = Path(ini_path).read_text(encoding="utf-8")

            self.assertIn(f"ExpertParameters={set_file.name}", content)
            self.assertIn(f"Report={report_file.name}", content)
            self.assertIn("Deposit=25000", content)
            self.assertIn("Currency=EUR", content)
            self.assertIn("Leverage=200", content)
            self.assertIn("Model=0", content)
            self.assertIn("ExecutionMode=1", content)
            self.assertNotIn("TestExpertParameters=", content)

    def test_copies_tester_report_to_workspace_report_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            target_report = root / "workspace" / "reports" / "trial.htm"
            config = root / "trial.ini"
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nReport=trial.htm\n",
                encoding="utf-8",
            )

            def finish_test(*args, **kwargs):
                (root / "trial.png").write_bytes(b"chart-image")
                (root / "trial.htm").write_text(
                    '<html><body><img src="trial.png"></body></html>',
                    encoding="utf-8",
                )
                return Mock(returncode=0, communicate=Mock(return_value=("", "")))

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen",
                side_effect=finish_test,
            ):
                result = MT5BacktestRunner(str(terminal)).run_backtest(
                    str(config), report_path=str(target_report)
                )

            self.assertTrue(result)
            self.assertEqual(
                target_report.read_text(encoding="utf-8"),
                '<html><body><img src="trial.png"></body></html>',
            )
            self.assertEqual(
                target_report.with_name("trial.png").read_bytes(), b"chart-image"
            )

    def test_fails_when_report_references_a_missing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            config = root / "trial.ini"
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nReport=trial.htm\n",
                encoding="utf-8",
            )

            def finish_test(*args, **kwargs):
                (root / "trial.htm").write_text(
                    '<html><body><img src="missing.png"></body></html>',
                    encoding="utf-8",
                )
                return Mock(returncode=0, communicate=Mock(return_value=("", "")))

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen",
                side_effect=finish_test,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "image"):
                    MT5BacktestRunner(str(terminal)).run_backtest(
                        str(config), report_path=str(root / "reports" / "trial.htm")
                    )

    def test_reports_missing_mt5_report_after_successful_terminal_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            config = root / "trial.ini"
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nReport=missing.htm\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as run_process:
                run_process.return_value.returncode = 0
                run_process.return_value.communicate.return_value = ("", "")
                with self.assertRaisesRegex(FileNotFoundError, "without creating"):
                    MT5BacktestRunner(str(terminal)).run_backtest(
                        str(config), report_path=str(root / "reports" / "missing.htm")
                    )

    def test_stages_expert_parameters_in_portable_tester_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            source_set = root / "workspace" / "trial.set"
            profile_set = root / "MQL5" / "Profiles" / "Tester" / "trial.set"
            config = root / "trial.ini"
            terminal.touch()
            source_set.parent.mkdir()
            source_set.write_text("CurrencyList=GBPUSDm\n", encoding="utf-8")
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nExpertParameters=trial.set\n",
                encoding="utf-8",
            )

            def start_terminal(*args, **kwargs):
                self.assertTrue(profile_set.is_file())
                return Mock(
                    returncode=0,
                    communicate=Mock(return_value=("", "")),
                )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as run_process:
                run_process.side_effect = start_terminal
                result = MT5BacktestRunner(str(terminal)).run_backtest(
                    str(config), set_file_path=str(source_set)
                )

            self.assertTrue(result)
            self.assertFalse(profile_set.exists())
            run_process.assert_called_once()

    def test_refuses_to_overwrite_existing_tester_profile_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            source_set = root / "workspace" / "trial.set"
            profile_set = root / "MQL5" / "Profiles" / "Tester" / "trial.set"
            config = root / "trial.ini"
            terminal.touch()
            source_set.parent.mkdir()
            source_set.write_text("CurrencyList=GBPUSDm\n", encoding="utf-8")
            profile_set.parent.mkdir(parents=True)
            profile_set.write_text("CurrencyList=GBPUSD\n", encoding="utf-8")
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nExpertParameters=trial.set\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as run_process:
                with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                    MT5BacktestRunner(str(terminal)).run_backtest(
                        str(config), set_file_path=str(source_set)
                    )
            run_process.assert_not_called()

    def test_requires_existing_profile_set_when_source_path_is_not_supplied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            config = root / "trial.ini"
            terminal.touch()
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n\n"
                "[Tester]\nExpertParameters=missing.set\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FileNotFoundError, "Profiles\\\\Tester"):
                MT5BacktestRunner(str(terminal)).run_backtest(str(config))

    def test_resolves_symbol_suffix_from_unique_broker_match(self):
        self.assertEqual(
            MT5ConfigBuilder.resolve_broker_symbol(
                "GBPUSDc", ["EURUSDm", "GBPUSDm", "USDJPYm"]
            ),
            "GBPUSDm",
        )

    def test_keeps_base_symbol_when_broker_has_no_suffix(self):
        self.assertEqual(
            MT5ConfigBuilder.resolve_broker_symbol(
                "GBPUSDc", ["EURUSD", "GBPUSD", "USDJPY"]
            ),
            "GBPUSD",
        )

    def test_keeps_exact_symbol_when_broker_has_multiple_suffixes(self):
        self.assertEqual(
            MT5ConfigBuilder.resolve_broker_symbol(
                "GBPUSDm", ["GBPUSDc", "GBPUSDm"]
            ),
            "GBPUSDm",
        )

    def test_gets_first_symbol_from_currency_list(self):
        self.assertEqual(
            MT5ConfigBuilder.get_first_currency_symbol(
                {"CurrencyList": {"value": "GBPUSDm;EURUSDm"}}
            ),
            "GBPUSDm",
        )

    def test_rejects_missing_first_currency_symbol(self):
        with self.assertRaisesRegex(ValueError, "first symbol"):
            MT5ConfigBuilder.get_first_currency_symbol(
                {"CurrencyList": {"value": ";EURUSDm"}}
            )

    def test_rejects_ambiguous_suffix_matches(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            MT5ConfigBuilder.resolve_broker_symbol(
                "GBPUSD", ["GBPUSDm", "GBPUSDc"]
            )

    def test_rejects_missing_broker_symbol(self):
        with self.assertRaisesRegex(ValueError, "does not provide"):
            MT5ConfigBuilder.resolve_broker_symbol("GBPUSDc", ["EURUSDm"])

    def test_resolves_currency_list_without_mutating_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.set"
            source.write_text(
                "CurrencyList=GBPUSDc;EURUSDc\n", encoding="utf-8"
            )
            builder = MT5ConfigBuilder(str(root / "workspace"))
            parsed = builder.parse_set_file(str(source))

            resolved = builder.resolve_broker_symbols(
                parsed, ["GBPUSDm", "EURUSDm"]
            )
            generated_set = Path(builder.build_set_file(parsed, {}, "resolved"))

            self.assertEqual(
                resolved, {"GBPUSDc": "GBPUSDm", "EURUSDc": "EURUSDm"}
            )
            self.assertIn("CurrencyList=GBPUSDm;EURUSDm", generated_set.read_text(encoding="utf-16"))
            self.assertIn("GBPUSDc;EURUSDc", source.read_text(encoding="utf-8"))

    def test_discovers_available_symbols_and_closes_only_started_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = Path(directory) / "terminal64.exe"
            terminal.touch()
            runner = MT5BacktestRunner(str(terminal))
            mt5 = Mock()
            mt5.initialize.return_value = True
            mt5.symbols_get.return_value = [
                type("Symbol", (), {"name": "EURUSDm"})(),
                type("Symbol", (), {"name": "GBPUSDm"})(),
            ]

            with (
                patch.dict(sys.modules, {"MetaTrader5": mt5}),
                patch.object(
                    runner,
                    "_terminal_process_ids",
                    side_effect=(set(), {4321}),
                ),
                patch.object(runner, "_close_terminal_process") as close_terminal,
            ):
                symbols = runner.get_available_symbols(
                    "1234", "test-secret", "Broker-Demo"
                )

            self.assertEqual(symbols, ["EURUSDm", "GBPUSDm"])
            mt5.initialize.assert_called_once_with(
                path=str(terminal),
                login=1234,
                password="test-secret",
                server="Broker-Demo",
                portable=True,
            )
            mt5.shutdown.assert_called_once()
            close_terminal.assert_called_once_with(4321)

    def test_launch_uses_login_from_ini_without_passing_password_on_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            config = root / "trial.ini"
            terminal.touch()
            config.write_text(
                "[Common]\n"
                "Login=001234\n"
                "Password=test-secret\n"
                "Server=Broker-Demo\n"
                "\n"
                "[Tester]\n"
                "Symbol=EURUSD\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as run_process:
                run_process.return_value.returncode = 0
                run_process.return_value.communicate.return_value = ("", "")

                result = MT5BacktestRunner(str(terminal)).run_backtest(str(config))

            self.assertTrue(result)
            command = run_process.call_args.args[0]
            self.assertIn("/login:001234", command)
            self.assertTrue(any(argument.startswith("/config:") for argument in command))
            self.assertNotIn("test-secret", command)
            run_process.return_value.communicate.assert_called_once_with(timeout=300)

    def test_launch_rejects_config_with_missing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            config = root / "trial.ini"
            terminal.touch()
            config.write_text(
                "[Common]\nLogin=1234\nServer=Broker-Demo\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as run_process:
                with self.assertRaisesRegex(ValueError, "Password"):
                    MT5BacktestRunner(str(terminal)).run_backtest(str(config))

            run_process.assert_not_called()

    def test_timeout_kills_terminal_and_waits_for_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            config = root / "trial.ini"
            terminal.touch()
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as start_process:
                process = start_process.return_value
                process.communicate.side_effect = [
                    subprocess.TimeoutExpired(cmd="terminal64.exe", timeout=1),
                    ("", ""),
                ]

                with self.assertRaisesRegex(TimeoutError, "1-second timeout"):
                    MT5BacktestRunner(str(terminal)).run_backtest(
                        str(config), timeout_seconds=1
                    )

                process.kill.assert_called_once()
                self.assertEqual(process.communicate.call_count, 2)

    def test_login_ini_contains_credentials_without_tester_section(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = MT5ConfigBuilder(directory)
            ini_path = builder.build_login_ini_file(
                "test", "1234", "test-secret", "Broker-Demo"
            )

            content = Path(ini_path).read_text(encoding="utf-8")
            self.assertIn("[Common]", content)
            self.assertIn("Login=1234", content)
            self.assertIn("Password=test-secret", content)
            self.assertIn("Server=Broker-Demo", content)
            self.assertNotIn("[Tester]", content)

    def test_login_terminal_uses_ini_and_waits_for_user_to_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            config = root / "login.ini"
            terminal.touch()
            config.write_text(
                "[Common]\nLogin=1234\nPassword=secret\nServer=Broker-Demo\n",
                encoding="utf-8",
            )

            with patch(
                "app.services.mt5_engine.runner.subprocess.Popen"
            ) as start_process:
                start_process.return_value.wait.return_value = 0

                result = MT5BacktestRunner(str(terminal)).open_login_terminal(
                    str(config)
                )

            self.assertTrue(result)
            command = start_process.call_args.args[0]
            self.assertIn("/login:1234", command)
            self.assertTrue(any(argument.startswith("/config:") for argument in command))
            self.assertNotIn("secret", command)
            start_process.return_value.wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
