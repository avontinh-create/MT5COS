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
        from_date: str = "2023.01.01",
        to_date: str = "2023.12.31",
    ) -> str:
        ini_content = f"""[Common]
Login={login}
Password={password}
Server={server}

[Tester]
Expert={expert_name}
Symbol={symbol}
Period={period}
Optimization=0
Model=1
FromDate={from_date}
ToDate={to_date}
TestExpertParameters={os.path.abspath(set_file_path)}
Report={os.path.abspath(report_path)}
ReplaceReport=1
ShutdownTerminal=0
"""
        ini_path = os.path.join(self.temp_configs_dir, f"trial_{trial_id}.ini")
        with open(ini_path, "w", encoding="utf-8", newline="") as ini_file:
            ini_file.write(ini_content)
        return ini_path
