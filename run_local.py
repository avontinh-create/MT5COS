
import argparse
import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from app.services.ai_engine.optimizer import AIOptimizer
from app.services.mt5_engine.builder import MT5ConfigBuilder
from app.services.mt5_engine.runner import MT5BacktestRunner


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local MT5 optimizer.")
    parser.add_argument(
        "--login-check",
        action="store_true",
        help="Open MT5 with .env credentials without starting the Strategy Tester.",
    )
    args = parser.parse_args(argv)

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

    if args.login_check:
        if offline_mode:
            raise RuntimeError("--login-check cannot be used with MT5_DRY_RUN enabled.")

        builder = MT5ConfigBuilder(str(project_root / "workspace"))
        login_ini_path = builder.build_login_ini_file(
            config_id=uuid.uuid4().hex,
            login=ACCOUNT_INFO["login"],
            password=ACCOUNT_INFO["password"],
            server=ACCOUNT_INFO["server"],
        )
        try:
            print(
                "[*] Opening MT5 for login verification. Close the terminal "
                "when you are done, then check its Journal for connection status."
            )
            MT5BacktestRunner(str(terminal_path)).open_login_terminal(login_ini_path)
        finally:
            try:
                os.remove(login_ini_path)
            except FileNotFoundError:
                pass
        return

    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(project_root / "workspace"),
        account_info=ACCOUNT_INFO,
        offline_mode=offline_mode,
    )

    available_symbols = None
    base_set_data = optimizer.builder.parse_set_file(SET_FILE_PATH)
    backtest_symbol = optimizer.builder.get_first_currency_symbol(base_set_data)
    if not offline_mode:
        available_symbols = optimizer.runner.get_available_symbols(
            ACCOUNT_INFO["login"],
            ACCOUNT_INFO["password"],
            ACCOUNT_INFO["server"],
        )
        backtest_symbol = optimizer.builder.resolve_broker_symbol(
            backtest_symbol, available_symbols
        )
        print(
            f"[*] Resolved tester symbol for {ACCOUNT_INFO['server']}: "
            f"{backtest_symbol}"
        )
    else:
        print(f"[*] Using first symbol from .set for tester: {backtest_symbol}")

    # 3. Nạp cấu hình
    print("[*] Loading configuration...")
    optimizer.setup_task(
        set_file_path=SET_FILE_PATH,
        expert_name=EXPERT_NAME,
        optimization_set_file_path=str(optimization_set_path),
        available_symbols=available_symbols,
    )
    if optimizer.resolved_currency_symbols:
        print(
            "[*] Resolved EA CurrencyList: "
            + ", ".join(
                f"{source} -> {resolved}"
                for source, resolved in optimizer.resolved_currency_symbols.items()
            )
        )
    
    # 4. Bắt đầu lặp 10 vòng
    print("[*] Starting optimization...")
    result = optimizer.run(n_trials=10, symbol=backtest_symbol)
    print(f"Best score: {result['best_value']}")
    print(f"Best parameters: {result['best_params']}")
    print(f"Best .set file: {result['best_set_file']}")
    for report_file in result["excel_reports"]:
        print(f"Excel report: {project_root / 'workspace' / 'reports' / report_file}")
    if result["mode"] == "offline_simulation":
        print(
            "[!] This only verifies the optimization flow; it is not an EA "
            "backtest result."
        )


if __name__ == "__main__":
    main()