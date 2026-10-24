
import os
from pathlib import Path

from dotenv import load_dotenv
from app.services.ai_engine.optimizer import AIOptimizer


def main():
    project_root = Path(__file__).resolve().parent
    load_dotenv(project_root / ".env")
    offline_mode = os.getenv("MT5_DRY_RUN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if offline_mode:
        terminal_path = project_root / "mt5_portable" / "terminal64.exe"
        print(
            "[!] OFFLINE SIMULATION: MT5 will not start; scores are synthetic "
            "and do not measure EA performance."
        )
    else:
        # Dùng một bản cài MT5 riêng để không ảnh hưởng terminal đang sử dụng.
        terminal_path = Path(
            os.getenv(
                "MT5_TERMINAL_PATH",
                str(project_root / "mt5_portable" / "terminal64.exe - Shortcut.lnk"),
            )
        )
        if not terminal_path.is_absolute():
            terminal_path = project_root / terminal_path
        if not terminal_path.is_file():
            raise FileNotFoundError(
                f"Không tìm thấy MT5 riêng tại: {terminal_path}. "
                "Hãy cài/copy MT5 vào thư mục riêng hoặc đặt MT5_TERMINAL_PATH trong .env."
            )

    # 2. Đường dẫn file .set gốc của bạn
    SET_FILE_PATH = str(project_root / "workspace" / "inputs" / "bot_config.set")
    optimization_set_path = Path(
        os.getenv(
            "MT5_OPTIMIZATION_SET",
            str(
                project_root
                / "workspace"
                / "inputs"
                / "optimization_ranges.set"
            ),
        )
    )
    if not optimization_set_path.is_absolute():
        optimization_set_path = project_root / optimization_set_path
    EXPERT_NAME = os.getenv("MT5_EXPERT_NAME")

    if not EXPERT_NAME:
        if offline_mode:
            EXPERT_NAME = "OfflineSimulation.ex5"
        else:
            raise RuntimeError(
                "Thiếu MT5_EXPERT_NAME trong .env. Hãy nhập tên EA .ex5 đã cài "
                "trong thư mục MQL5\\Experts."
            )
    
    # 3. Thông tin tài khoản MT5
    required_account_vars = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    missing_account_vars = [name for name in required_account_vars if not os.getenv(name)]
    if missing_account_vars and not offline_mode:
        raise RuntimeError(
            f"Thiếu biến đăng nhập MT5: {', '.join(missing_account_vars)}. "
            "Hãy kiểm tra file .env."
        )

    ACCOUNT_INFO = {
        "login": os.getenv("MT5_LOGIN", ""),
        "password": os.getenv("MT5_PASSWORD", ""),
        "server": os.getenv("MT5_SERVER", ""),
    }

    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(project_root / "workspace"),
        account_info=ACCOUNT_INFO,
        offline_mode=offline_mode,
    )

    # 3. Nạp cấu hình
    print("[*] Loading configuration...")
    optimizer.setup_task(
        set_file_path=SET_FILE_PATH,
        expert_name=EXPERT_NAME,
        optimization_set_file_path=str(optimization_set_path),
    )
    
    # 4. Bắt đầu lặp 10 vòng
    print("[*] Starting optimization...")
    result = optimizer.run(n_trials=10)
    print(f"Best score: {result['best_value']}")
    print(f"Best parameters: {result['best_params']}")
    print(f"Best .set file: {result['best_set_file']}")
    if result["mode"] == "offline_simulation":
        print(
            "[!] This only verifies the optimization flow; it is not an EA "
            "backtest result."
        )


if __name__ == "__main__":
    main()