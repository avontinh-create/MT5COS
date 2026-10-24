import math
import os
import re
import uuid

import optuna

from app.services.mt5_engine.builder import MT5ConfigBuilder
from app.services.mt5_engine.parser import ReportParser
from app.services.mt5_engine.runner import MT5BacktestRunner


ARRAY_OPTIMIZATION_RELATIVE_PERCENT = 0.12


class AIOptimizer:
    def __init__(
        self,
        terminal_path: str,
        base_workspace: str,
        account_info: dict,
        offline_mode: bool = False,
    ):
        self.builder = MT5ConfigBuilder(base_workspace)
        self.runner = MT5BacktestRunner(terminal_path)
        self.parser = ReportParser()
        self.account_info = account_info
        self.offline_mode = offline_mode
        self.reports_dir = os.path.join(base_workspace, "reports")
        os.makedirs(self.reports_dir, exist_ok=True)

        self.base_set_data = {}
        self.optimizable_params = {}
        self.expert_name = ""

    def setup_task(
        self,
        set_file_path: str,
        expert_name: str,
        optimization_set_file_path: str | None = None,
    ):
        if not os.path.isfile(set_file_path):
            raise FileNotFoundError(f"Input .set file not found: {set_file_path}")
        if not set_file_path.lower().endswith(".set"):
            raise ValueError("Input parameter file must have a .set extension.")
        if optimization_set_file_path is not None:
            if not os.path.isfile(optimization_set_file_path):
                raise FileNotFoundError(
                    "Optimization range .set file not found: "
                    f"{optimization_set_file_path}"
                )
            if not optimization_set_file_path.lower().endswith(".set"):
                raise ValueError("Optimization range file must have a .set extension.")
        if not expert_name or "\n" in expert_name or "\r" in expert_name:
            raise ValueError("A valid EA expert name is required.")
        if not expert_name.lower().endswith(".ex5"):
            raise ValueError("EA expert name must end with .ex5.")

        self.base_set_data = self.builder.parse_set_file(set_file_path)
        optimization_data = self.base_set_data
        if optimization_set_file_path is not None:
            optimization_data = self.builder.parse_set_file(
                optimization_set_file_path
            )

        self.optimizable_params = self.builder.get_optimizable_params(
            optimization_data
        )
        for parameter, setting in optimization_data.items():
            match = re.fullmatch(r"(.+)\[(\d+)\]", parameter)
            if match is None or not setting["value"].strip():
                continue

            base_key, raw_index = match.groups()
            if setting["value"].strip().upper() not in {"Y", "N"}:
                raise ValueError(
                    f"Array optimization selector {parameter} must be Y or N."
                )
            if setting["value"].strip().upper() == "N":
                continue
            if base_key not in self.base_set_data:
                raise ValueError(
                    f"Array optimization selector refers to missing parameter "
                    f"{base_key}."
                )

            index = int(raw_index)
            if index < 1:
                raise ValueError(
                    f"Array optimization index must be 1-based: {parameter}."
                )
            elements = self.base_set_data[base_key]["value"].split(";")
            if index > len(elements):
                raise ValueError(
                    f"Array optimization index {index} is outside {base_key}, "
                    f"which has {len(elements)} values."
                )

            try:
                center = float(elements[index - 1].strip())
            except ValueError as exc:
                raise ValueError(
                    f"Selected array value {parameter} must be numeric."
                ) from exc
            if not math.isfinite(center):
                raise ValueError(f"Selected array value {parameter} must be finite.")
            if center == 0:
                raise ValueError(
                    f"Selected array value {parameter} is zero and zero-valued "
                    "positions are excluded from automatic relative optimization."
                )

            radius = abs(center) * ARRAY_OPTIMIZATION_RELATIVE_PERCENT
            limits = {
                "start": float(math.ceil(center - radius)),
                "step": 1.0,
                "stop": float(math.floor(center + radius)),
            }
            if limits["start"] >= limits["stop"]:
                raise ValueError(
                    f"Selected array value {parameter} has no integer alternatives "
                    "within the configured 12% range."
                )
            self.optimizable_params[parameter] = limits

        if not self.optimizable_params:
            raise ValueError(
                "The .set file has no parameters enabled for optimization. "
                "Mark at least one numeric range with a final Y, or enable an "
                "array position with Parameter[index]=Y."
            )

        unknown_params = {
            key
            for key in self.optimizable_params
            if not re.fullmatch(r".+\[\d+\]", key)
            and key not in self.base_set_data
        }
        if unknown_params:
            raise ValueError(
                "Optimization range file contains parameters missing from the "
                "base .set file: "
                + ", ".join(sorted(unknown_params))
            )
        for key in self.optimizable_params:
            match = re.fullmatch(r"(.+)\[(\d+)\]", key)
            if match:
                continue
            try:
                float(self.base_set_data[key]["value"])
            except ValueError as exc:
                raise ValueError(
                    f"Base value for optimized parameter {key} must be numeric."
                ) from exc

        self.expert_name = expert_name.strip()

    def objective(
        self,
        trial: optuna.Trial,
        run_id: str,
        symbol: str,
        period: str,
        from_date: str,
        to_date: str,
    ) -> float:
        new_values = {}
        for key, limits in self.optimizable_params.items():
            match = re.fullmatch(r"(.+)\[(\d+)\]", key)
            if match:
                base_key, raw_index = match.groups()
                original_value = self.base_set_data[base_key]["value"].split(";")[
                    int(raw_index) - 1
                ]
            else:
                original_value = self.base_set_data[key]["value"]
            is_integer = all(
                value.is_integer()
                for value in (
                    float(original_value),
                    limits["start"],
                    limits["step"],
                    limits["stop"],
                )
            )
            if is_integer:
                new_values[key] = trial.suggest_int(
                    key,
                    int(limits["start"]),
                    int(limits["stop"]),
                    step=int(limits["step"]),
                )
            else:
                new_values[key] = trial.suggest_float(
                    key,
                    limits["start"],
                    limits["stop"],
                    step=limits["step"],
                )

        trial_id = f"{run_id}_{trial.number}"
        set_path = self.builder.build_set_file(
            self.base_set_data, new_values, trial_id
        )
        if self.offline_mode:
            score = 0.0
            for key, value in new_values.items():
                limits = self.optimizable_params[key]
                midpoint = (limits["start"] + limits["stop"]) / 2
                half_range = (limits["stop"] - limits["start"]) / 2
                if half_range:
                    normalized_distance = (float(value) - midpoint) / half_range
                    score -= normalized_distance**2
            return score

        report_path = os.path.join(self.reports_dir, f"report_trial_{trial_id}.htm")
        ini_path = self.builder.build_ini_file(
            trial_id=trial_id,
            expert_name=self.expert_name,
            symbol=symbol,
            period=period,
            set_file_path=set_path,
            report_path=report_path,
            login=self.account_info["login"],
            password=self.account_info["password"],
            server=self.account_info["server"],
            from_date=from_date,
            to_date=to_date,
        )

        try:
            if not self.runner.run_backtest(ini_path):
                raise RuntimeError(
                    "MT5 exited with a non-zero status for optimization "
                    f"trial {trial.number}."
                )

            metrics = self.parser.parse_html_report(report_path)
            drawdown = max(metrics["MaxDrawdown"], 0.1)
            score = (metrics["NetProfit"] * metrics["ProfitFactor"]) / drawdown
            if not math.isfinite(score):
                raise ValueError(
                    "MT5 report produced a non-finite score for "
                    f"trial {trial.number}."
                )
            return score
        finally:
            try:
                os.remove(ini_path)
            except FileNotFoundError:
                pass

    def run(
        self,
        n_trials: int = 10,
        symbol: str = "EURUSD",
        period: str = "H1",
        from_date: str = "2023.01.01",
        to_date: str = "2023.12.31",
    ) -> dict:
        if not self.base_set_data or not self.optimizable_params:
            raise RuntimeError("Call setup_task with an optimizable .set file first.")
        if n_trials < 1:
            raise ValueError("n_trials must be greater than zero.")
        for label, value in (
            ("symbol", symbol),
            ("period", period),
            ("from_date", from_date),
            ("to_date", to_date),
        ):
            if not value or "\n" in value or "\r" in value:
                raise ValueError(f"{label} must be a non-empty single-line value.")

        run_id = uuid.uuid4().hex
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(
                n_startup_trials=min(5, n_trials)
            ),
        )
        study.optimize(
            lambda trial: self.objective(
                trial, run_id, symbol, period, from_date, to_date
            ),
            n_trials=n_trials,
        )

        best_set = self.builder.build_set_file(
            self.base_set_data, study.best_params, f"{run_id}_BEST_RESULT"
        )
        return {
            "best_value": study.best_value,
            "best_params": study.best_params,
            "best_set_file": best_set,
            "mode": "offline_simulation" if self.offline_mode else "mt5",
        }
