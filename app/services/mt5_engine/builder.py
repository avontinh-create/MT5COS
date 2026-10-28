import math
import os
import re


class MT5ConfigBuilder:
    def __init__(self, workspace_dir: str = "./workspace"):
        self.temp_configs_dir = os.path.join(workspace_dir, "temp_configs")
        os.makedirs(self.temp_configs_dir, exist_ok=True)

    def parse_set_file(self, file_path: str) -> dict:
        with open(file_path, "rb") as set_file:
            content = set_file.read()

        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = content.decode("utf-16")
        elif content.startswith(b"\xef\xbb\xbf"):
            text = content.decode("utf-8-sig")
        elif b"\x00" in content[:100]:
            text = content.decode("utf-16-le")
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("cp1252")

        parsed_data = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith(";") or "=" not in line:
                continue

            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key:
                raise ValueError(f"Parameter name is empty on line {line_number}.")
            if key in parsed_data:
                raise ValueError(f"Duplicate parameter name on line {line_number}: {key}")

            parts = raw_value.split("||")
            if len(parts) == 1:
                parsed_data[key] = {"value": parts[0], "is_complex": False}
            else:
                if len(parts) != 5:
                    raise ValueError(
                        f"Invalid optimization settings for parameter {key} "
                        f"on line {line_number}; expected value||start||step||stop||Y/N."
                    )
                optimize_flag = parts[4].strip().upper()
                if optimize_flag not in {"Y", "N"}:
                    raise ValueError(
                        f"Invalid optimization flag for parameter {key} "
                        f"on line {line_number}; expected Y or N."
                    )
                parsed_data[key] = {
                    "value": parts[0],
                    "start": parts[1],
                    "step": parts[2],
                    "stop": parts[3],
                    "optimize_flag": optimize_flag,
                    "is_complex": True,
                }

        if not parsed_data:
            raise ValueError("The uploaded .set file does not contain any parameters.")
        return parsed_data

    def get_optimizable_params(self, parsed_data: dict) -> dict:
        optimizable = {}
        for key, data in parsed_data.items():
            if not data.get("is_complex") or data["optimize_flag"] != "Y":
                continue

            try:
                value = float(data["value"])
                start = float(data["start"])
                step = float(data["step"])
                stop = float(data["stop"])
            except ValueError as exc:
                raise ValueError(
                    f"Current value and optimization range for {key} must be numeric."
                ) from exc

            if not all(
                math.isfinite(number) for number in (value, start, step, stop)
            ):
                raise ValueError(
                    f"Optimization value and range for {key} must be finite."
                )
            if step <= 0 or stop < start or (stop > start and step > stop - start):
                raise ValueError(
                    f"Invalid optimization range for {key}: require start <= stop "
                    "and a positive step no larger than the range."
                )

            optimizable[key] = {"start": start, "step": step, "stop": stop}

        return optimizable

    @staticmethod
    def get_first_currency_symbol(parsed_data: dict) -> str:
        currency_list = parsed_data.get("CurrencyList")
        if currency_list is None:
            raise ValueError("The .set file does not contain CurrencyList.")

        symbols = [symbol.strip() for symbol in currency_list["value"].split(";")]
        if not symbols or not symbols[0]:
            raise ValueError("CurrencyList does not contain a first symbol.")
        return symbols[0]

    @staticmethod
    def resolve_broker_symbol(symbol: str, available_symbols: list[str]) -> str:
        normalized_symbol = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z]{6}[^;\s]*", normalized_symbol):
            raise ValueError(
                f"Cannot automatically resolve broker suffix for symbol {symbol!r}."
            )

        exact_matches = [
            available for available in available_symbols
            if available.upper() == normalized_symbol
        ]
        if exact_matches:
            return exact_matches[0]

        base_symbol = normalized_symbol[:6]
        base_matches = [
            available for available in available_symbols
            if available.upper() == base_symbol
        ]
        if base_matches:
            return base_matches[0]

        candidates = sorted(
            {
                available
                for available in available_symbols
                if available.upper().startswith(base_symbol)
                and len(available) > len(base_symbol)
            }
        )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(
                f"Broker does not provide a symbol matching {base_symbol}."
            )
        raise ValueError(
            f"Broker symbol for {base_symbol} is ambiguous: "
            + ", ".join(candidates)
        )

    def resolve_broker_symbols(
        self, parsed_data: dict, available_symbols: list[str]
    ) -> dict[str, str]:
        currency_list = parsed_data.get("CurrencyList")
        if currency_list is None:
            return {}

        symbols = [symbol.strip() for symbol in currency_list["value"].split(";")]
        if not symbols or any(not symbol for symbol in symbols):
            raise ValueError("CurrencyList contains an empty symbol.")

        resolved = {
            symbol: self.resolve_broker_symbol(symbol, available_symbols)
            for symbol in symbols
        }
        currency_list["value"] = ";".join(resolved.values())
        return resolved

    def build_set_file(self, original_parsed: dict, new_values: dict, trial_id) -> str:
        indexed_values = {}
        for parameter, value in new_values.items():
            match = re.fullmatch(r"(.+)\[(\d+)\]", parameter)
            if match is None:
                continue
            base_key, raw_index = match.groups()
            indexed_values.setdefault(base_key, {})[int(raw_index) - 1] = value

        out_path = os.path.join(self.temp_configs_dir, f"trial_{trial_id}.set")
        with open(out_path, "w", encoding="utf-16", newline="") as set_file:
            for key, data in original_parsed.items():
                value = new_values.get(key, data["value"])
                if key in indexed_values:
                    elements = value.split(";")
                    for index, replacement in indexed_values[key].items():
                        if index < 0 or index >= len(elements):
                            raise ValueError(
                                f"Optimization index {index + 1} is outside "
                                f"parameter {key}, which has {len(elements)} values."
                            )
                        elements[index] = str(replacement)
                    value = ";".join(elements)
                if data.get("is_complex"):
                    set_file.write(
                        f"{key}={value}||{data['start']}||{data['step']}||"
                        f"{data['stop']}||{data['optimize_flag']}\r\n"
                    )
                else:
                    set_file.write(f"{key}={value}\r\n")
        return out_path

    def build_ini_file(
        self,
        trial_id,
        expert_name: str,
        symbol: str,
        period: str,
        set_file_path: str,
        report_path: str,
        login: str,
        password: str,
        server: str,
        from_date: str = "2026.01.01",
        to_date: str = "2026.12.31",
        deposit: float = 10000,
        currency: str = "USD",
        leverage: int = 100,
        model: int = 1,
        execution_mode: int = 0,
    ) -> str:
        if deposit <= 0 or not math.isfinite(deposit):
            raise ValueError("MT5 tester deposit must be a positive finite number.")
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
        ):
            raise ValueError("MT5 tester currency must be a three-letter code.")
        if leverage < 1:
            raise ValueError("MT5 tester leverage must be greater than zero.")
        if model not in {0, 1, 2, 4}:
            raise ValueError(
                "MT5 tester model must be 0, 1, 2, or 4 for financial testing."
            )
        if execution_mode not in {0, 1}:
            raise ValueError("MT5 execution mode must be 0 (no delay) or 1 (random).")

        ini_content = f"""[Common]
Login={login}
Password={password}
Server={server}

[Tester]
Expert={expert_name}
Symbol={symbol}
Period={period}
Optimization=0
Model={model}
FromDate={from_date}
ToDate={to_date}
Deposit={deposit:g}
Currency={currency.upper()}
Leverage={leverage}
ExecutionMode={execution_mode}
ExpertParameters={os.path.basename(set_file_path)}
Report={os.path.basename(report_path)}
ReplaceReport=1
ShutdownTerminal=1
"""
        ini_path = os.path.join(self.temp_configs_dir, f"trial_{trial_id}.ini")
        with open(ini_path, "w", encoding="utf-8", newline="") as ini_file:
            ini_file.write(ini_content)
        return ini_path

    def build_login_ini_file(
        self, config_id, login: str, password: str, server: str
    ) -> str:
        for label, value in (
            ("login", login),
            ("password", password),
            ("server", server),
        ):
            if not value or "\n" in value or "\r" in value:
                raise ValueError(f"MT5 {label} must be non-empty and single-line.")

        ini_content = f"""[Common]
Login={login}
Password={password}
Server={server}
"""
        ini_path = os.path.join(self.temp_configs_dir, f"login_{config_id}.ini")
        with open(ini_path, "w", encoding="utf-8", newline="") as ini_file:
            ini_file.write(ini_content)
        return ini_path
