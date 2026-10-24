import json
import os
import shlex
import shutil
import subprocess
import threading


class MT5BacktestRunner:
    _terminal_lock = threading.Lock()

    def __init__(self, terminal_path: str):
        self.terminal_path = terminal_path

    def _resolve_shortcut(self) -> tuple[str, list[str], str]:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise RuntimeError("PowerShell is required to resolve an MT5 .lnk shortcut.")

        env = os.environ.copy()
        env["MT5_SHORTCUT_PATH"] = os.path.abspath(self.terminal_path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut("
            "$env:MT5_SHORTCUT_PATH); "
            "[PSCustomObject]@{TargetPath=$shortcut.TargetPath; "
            "Arguments=$shortcut.Arguments; "
            "WorkingDirectory=$shortcut.WorkingDirectory} | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not resolve MT5 shortcut: {result.stderr.strip()}"
            )

        try:
            shortcut = json.loads(result.stdout)
            target_path = shortcut["TargetPath"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("MT5 shortcut did not provide a valid target path.") from exc

        arguments = shlex.split(shortcut.get("Arguments") or "", posix=False)
        arguments = [argument.strip('"') for argument in arguments]
        working_directory = shortcut.get("WorkingDirectory") or os.path.dirname(
            target_path
        )
        return target_path, arguments, working_directory

    def run_backtest(self, ini_path: str, timeout_seconds: int = 300) -> bool:
        if not os.path.isfile(self.terminal_path):
            raise FileNotFoundError(f"MetaTrader terminal not found: {self.terminal_path}")
        if not os.path.isfile(ini_path):
            raise FileNotFoundError(f"Backtest configuration not found: {ini_path}")
        if timeout_seconds <= 0:
            raise ValueError("Backtest timeout must be greater than zero.")

        if self.terminal_path.lower().endswith(".lnk"):
            terminal_path, shortcut_arguments, working_directory = (
                self._resolve_shortcut()
            )
        else:
            terminal_path = self.terminal_path
            shortcut_arguments = []
            working_directory = os.path.dirname(os.path.abspath(terminal_path))

        if not os.path.isfile(terminal_path):
            raise FileNotFoundError(
                f"MetaTrader executable from shortcut not found: {terminal_path}"
            )

        portable_argument = "/portable"
        if not any(
            argument.lower() == portable_argument
            for argument in shortcut_arguments
        ):
            shortcut_arguments.append(portable_argument)

        try:
            with self._terminal_lock:
                result = subprocess.run(
                    [
                        terminal_path,
                        *shortcut_arguments,
                        f"/config:{os.path.abspath(ini_path)}",
                    ],
                    cwd=working_directory,
                    check=False,
                    timeout=timeout_seconds,
                    capture_output=True,
                    text=True,
                )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"MT5 backtest exceeded the {timeout_seconds}-second timeout."
            ) from exc
        return result.returncode == 0
