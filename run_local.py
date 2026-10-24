
import os
from pathlib import Path

from dotenv import load_dotenv
from app.services.ai_engine.optimizer import AIOptimizer


def main():
    project_root = Path(__file__).resolve().parent
    load_dotenv(project_root / ".env")

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
        raise RuntimeError(
            "Thiếu MT5_EXPERT_NAME trong .env. Hãy nhập tên EA .ex5 đã cài "
            "trong thư mục MQL5\\Experts."
        )
    
    # 3. Thông tin tài khoản MT5
    required_account_vars = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    missing_account_vars = [name for name in required_account_vars if not os.getenv(name)]
    if missing_account_vars:
        raise RuntimeError(
            f"Thiếu biến đăng nhập MT5: {', '.join(missing_account_vars)}. "
            "Hãy kiểm tra file .env."
        )

    ACCOUNT_INFO = {
        "login": os.environ["MT5_LOGIN"],
        "password": os.environ["MT5_PASSWORD"],
        "server": os.environ["MT5_SERVER"],
    }

    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(project_root / "workspace"),
        account_info=ACCOUNT_INFO,
    )

    # 3. Nạp cấu hình
    print("[*] Đang nạp cấu hình và thiết lập AI...")
    optimizer.setup_task(
        set_file_path=SET_FILE_PATH,
        expert_name=EXPERT_NAME,
        optimization_set_file_path=str(optimization_set_path),
    )
    
    # 4. Bắt đầu lặp 10 vòng
    print("[*] Bắt đầu chạy Backtest Tự động...")
    optimizer.run(n_trials=10)


if __name__ == "__main__":
    main()