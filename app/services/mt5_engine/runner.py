import subprocess
import os

class MT5BacktestRunner:
    def __init__(self, terminal_path: str):
        self.terminal_path = terminal_path

    def run_backtest(self, config_ini_path: str, timeout_seconds: int = 300) -> bool:
        command = [self.terminal_path, f"/config:{config_ini_path}", "/portable"]
        try:
            print(f"[*] Chạy MT5: {config_ini_path}")
            process = subprocess.run(command, timeout=timeout_seconds, capture_output=True, text=True)
            return process.returncode == 0
        except subprocess.TimeoutExpired:
            os.system("taskkill /f /im terminal64.exe")
            return False
