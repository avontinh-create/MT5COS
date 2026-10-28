import configparser
import json
import os
import shlex
import shutil
import subprocess
import threading
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit


class _ReportImageSourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "img":
            return
        source = dict(attrs).get("src")
        if source:
            self.sources.append(source)


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

    @staticmethod
    def _terminal_process_ids(terminal_path: str) -> set[int]:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise RuntimeError(
                "PowerShell is required to manage MT5 symbol discovery startup."
            )

        env = os.environ.copy()
        env["MT5_TERMINAL_EXE"] = os.path.abspath(terminal_path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$ids = @(Get-CimInstance Win32_Process -Filter "
            "\"Name = 'terminal64.exe'\" | "
            "Where-Object { $_.ExecutablePath -eq $env:MT5_TERMINAL_EXE } | "
            "Select-Object -ExpandProperty ProcessId); "
            "ConvertTo-Json -Compress -InputObject $ids"
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
                "Could not inspect MT5 processes before broker symbol discovery."
            )
        try:
            process_ids = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Could not parse MT5 process list for broker symbol discovery."
            ) from exc

        if isinstance(process_ids, int):
            return {process_ids}
        if process_ids is None:
            return set()
        return {int(process_id) for process_id in process_ids}

    @staticmethod
    def _close_terminal_process(process_id: int) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise RuntimeError(
                "PowerShell is required to close the temporary MT5 symbol session."
            )

        env = os.environ.copy()
        env["MT5_PROCESS_ID"] = str(process_id)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$process = Get-Process -Id ([int]$env:MT5_PROCESS_ID) "
            "-ErrorAction SilentlyContinue; "
            "if ($process) { "
            "if (-not $process.CloseMainWindow()) { exit 2 }; "
            "if (-not $process.WaitForExit(15000)) { exit 3 } }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode == 2:
            raise RuntimeError(
                "Could not gracefully close the temporary MT5 symbol session."
            )
        if result.returncode == 3:
            raise TimeoutError(
                "Temporary MT5 terminal did not close after symbol discovery."
            )
        if result.returncode != 0:
            raise RuntimeError(
                "Failed while closing the temporary MT5 symbol session."
            )

    def get_available_symbols(
        self, login: str, password: str, server: str
    ) -> list[str]:
        if not login.isdigit():
            raise ValueError("MT5 login must contain only digits.")
        if not password or not server:
            raise ValueError(
                "MT5 password and server are required for symbol discovery."
            )

        if self.terminal_path.lower().endswith(".lnk"):
            terminal_path, _, _ = self._resolve_shortcut()
        else:
            terminal_path = os.path.abspath(self.terminal_path)
        if not os.path.isfile(terminal_path):
            raise FileNotFoundError(
                f"MetaTrader executable not found: {terminal_path}"
            )

        existing_processes = self._terminal_process_ids(terminal_path)
        if existing_processes:
            raise RuntimeError(
                "Close the portable MT5 terminal before automatic symbol discovery."
            )

        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError(
                "Install the MetaTrader5 Python package to discover broker symbols."
            ) from exc

        initialized = False
        try:
            initialized = mt5.initialize(
                path=terminal_path,
                login=int(login),
                password=password,
                server=server,
                portable=True,
            )
            if not initialized:
                raise RuntimeError(
                    "Could not initialize MT5 for symbol discovery: "
                    f"{mt5.last_error()}"
                )

            symbols = mt5.symbols_get()
            if symbols is None:
                raise RuntimeError(
                    "Could not read symbols from MT5: "
                    f"{mt5.last_error()}"
                )
            available_symbols = sorted(
                {symbol.name for symbol in symbols if getattr(symbol, "name", "")}
            )
            if not available_symbols:
                raise RuntimeError("MT5 returned an empty broker symbol list.")
            return available_symbols
        finally:
            try:
                if initialized:
                    mt5.shutdown()
            finally:
                started_processes = (
                    self._terminal_process_ids(terminal_path) - existing_processes
                )
                for process_id in started_processes:
                    self._close_terminal_process(process_id)

    @staticmethod
    def _read_account_login(ini_path: str) -> str:
        config = configparser.ConfigParser(interpolation=None)
        try:
            with open(ini_path, encoding="utf-8-sig") as ini_file:
                config.read_file(ini_file)
        except (OSError, configparser.Error) as exc:
            raise ValueError(f"Could not read MT5 config file: {ini_path}") from exc

        if not config.has_section("Common"):
            raise ValueError("MT5 config is missing the [Common] section.")

        account = config["Common"]
        missing = [
            key for key in ("Login", "Password", "Server") if not account.get(key, "").strip()
        ]
        if missing:
            raise ValueError(
                "MT5 config is missing required [Common] fields: "
                + ", ".join(missing)
            )

        return account["Login"].strip()

    @staticmethod
    def _copy_report_images(report_source: str, report_destination: str) -> None:
        with open(report_source, "rb") as report_file:
            content = report_file.read()
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            html = content.decode("utf-16")
        elif content.startswith(b"\xef\xbb\xbf"):
            html = content.decode("utf-8-sig")
        else:
            try:
                html = content.decode("utf-8")
            except UnicodeDecodeError:
                html = content.decode("cp1252")

        parser = _ReportImageSourceParser()
        parser.feed(html)
        source_root = os.path.realpath(os.path.dirname(report_source))
        destination_root = os.path.dirname(os.path.abspath(report_destination))

        for source in parser.sources:
            parsed_source = urlsplit(source)
            if parsed_source.scheme or parsed_source.netloc or not parsed_source.path:
                continue
            relative_path = unquote(parsed_source.path).replace("/", os.sep)
            source_path = os.path.realpath(os.path.join(source_root, relative_path))
            if os.path.commonpath((source_root, source_path)) != source_root:
                continue
            if not os.path.isfile(source_path):
                raise FileNotFoundError(
                    f"MT5 report image referenced by {report_source} was not created: "
                    f"{source}"
                )

            destination_path = os.path.join(destination_root, relative_path)
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            shutil.copy2(source_path, destination_path)

    def run_backtest(
        self,
        ini_path: str,
        timeout_seconds: int = 300,
        set_file_path: str | None = None,
        report_path: str | None = None,
    ) -> bool:
        if not os.path.isfile(self.terminal_path):
            raise FileNotFoundError(f"MetaTrader terminal not found: {self.terminal_path}")
        if not os.path.isfile(ini_path):
            raise FileNotFoundError(f"Backtest configuration not found: {ini_path}")
        if timeout_seconds <= 0:
            raise ValueError("Backtest timeout must be greater than zero.")

        account_login = self._read_account_login(ini_path)

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
        shortcut_arguments = [
            argument
            for argument in shortcut_arguments
            if not argument.lower().startswith("/login:")
        ]
        shortcut_arguments.append(f"/login:{account_login}")

        staged_set_path = None
        config = configparser.ConfigParser(interpolation=None)
        try:
            with open(ini_path, encoding="utf-8-sig") as ini_file:
                config.read_file(ini_file)
        except (OSError, configparser.Error) as exc:
            raise ValueError(f"Could not read MT5 config file: {ini_path}") from exc

        expert_parameters = config.get("Tester", "ExpertParameters", fallback="").strip()
        tester_profile_dir = os.path.join(
            os.path.dirname(os.path.abspath(terminal_path)),
            "MQL5",
            "Profiles",
            "Tester",
        )
        if expert_parameters and not expert_parameters.lower().endswith(".set"):
            raise ValueError("MT5 ExpertParameters must name a .set file.")
        if expert_parameters and os.path.basename(
            expert_parameters
        ) != expert_parameters:
            raise ValueError(
                "MT5 ExpertParameters must be a file name in MQL5\\Profiles\\Tester."
            )

        source_set_path = None
        if set_file_path is not None:
            source_set_path = os.path.abspath(set_file_path)

        if source_set_path is not None:
            if not os.path.isfile(source_set_path):
                raise FileNotFoundError(
                    f"Expert parameter .set file not found: {source_set_path}"
                )
            if not expert_parameters or os.path.basename(
                expert_parameters
            ).casefold() != os.path.basename(source_set_path).casefold():
                raise ValueError(
                    "MT5 ExpertParameters must match the supplied .set file name."
                )
            os.makedirs(tester_profile_dir, exist_ok=True)
            staged_set_path = os.path.join(
                tester_profile_dir, os.path.basename(source_set_path)
            )
            if os.path.exists(staged_set_path) and os.path.samefile(
                source_set_path, staged_set_path
            ):
                staged_set_path = None
            elif os.path.exists(staged_set_path):
                raise FileExistsError(
                    "Refusing to overwrite an existing MT5 tester parameter file: "
                    f"{staged_set_path}"
                )
            else:
                shutil.copyfile(source_set_path, staged_set_path)
        elif expert_parameters:
            profile_set_path = os.path.join(
                tester_profile_dir, expert_parameters
            )
            if not os.path.isfile(profile_set_path):
                raise FileNotFoundError(
                    "MT5 tester parameter file is missing from MQL5\\Profiles\\Tester: "
                    f"{profile_set_path}"
                )

        report_name = config.get("Tester", "Report", fallback="").strip()
        if report_path is not None:
            target_report_path = os.path.abspath(report_path)
            if (
                not report_name
                or os.path.basename(report_name) != report_name
                or os.path.splitext(report_name)[1].lower() not in {".htm", ".html"}
            ):
                raise ValueError(
                    "MT5 Report must be an .htm or .html file name."
                )
            if (
                report_name.casefold()
                != os.path.basename(target_report_path).casefold()
            ):
                raise ValueError(
                    "MT5 Report must match the requested report file name."
                )
            terminal_root = os.path.dirname(os.path.abspath(terminal_path))
            terminal_report_dirs = list(
                dict.fromkeys(
                    (
                        os.path.join(terminal_root, "Reports"),
                        working_directory,
                        terminal_root,
                    )
                )
            )

        with self._terminal_lock:
            try:
                try:
                    process = subprocess.Popen(
                        [
                            terminal_path,
                            *shortcut_arguments,
                            f"/config:{os.path.abspath(ini_path)}",
                        ],
                        cwd=working_directory,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not start MetaTrader terminal: {terminal_path}"
                    ) from exc
                try:
                    process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.communicate()
                    raise TimeoutError(
                        f"MT5 backtest exceeded the {timeout_seconds}-second timeout."
                    ) from exc
                if process.returncode == 0 and report_path is not None:
                    report_stem = os.path.splitext(report_name)[0]
                    source_candidates = [
                        os.path.join(directory, report_stem + extension)
                        for directory in terminal_report_dirs
                        for extension in (".htm", ".html")
                    ]
                    existing_report = next(
                        (
                            candidate
                            for candidate in dict.fromkeys(source_candidates)
                            if os.path.isfile(candidate)
                        ),
                        None,
                    )
                    if existing_report is None:
                        raise FileNotFoundError(
                            "MT5 finished without creating its report in "
                            f"{', '.join(terminal_report_dirs)}: {report_name}"
                        )
                    os.makedirs(os.path.dirname(target_report_path), exist_ok=True)
                    self._copy_report_images(existing_report, target_report_path)
                    shutil.copy2(existing_report, target_report_path)
            finally:
                if staged_set_path is not None:
                    try:
                        os.remove(staged_set_path)
                    except FileNotFoundError:
                        pass
        return process.returncode == 0

    def open_login_terminal(self, ini_path: str) -> bool:
        if not os.path.isfile(self.terminal_path):
            raise FileNotFoundError(f"MetaTrader terminal not found: {self.terminal_path}")
        if not os.path.isfile(ini_path):
            raise FileNotFoundError(f"Login configuration not found: {ini_path}")

        account_login = self._read_account_login(ini_path)
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

        if not any(
            argument.lower() == "/portable" for argument in shortcut_arguments
        ):
            shortcut_arguments.append("/portable")
        shortcut_arguments = [
            argument
            for argument in shortcut_arguments
            if not argument.lower().startswith("/login:")
        ]
        shortcut_arguments.append(f"/login:{account_login}")

        with self._terminal_lock:
            try:
                process = subprocess.Popen(
                    [
                        terminal_path,
                        *shortcut_arguments,
                        f"/config:{os.path.abspath(ini_path)}",
                    ],
                    cwd=working_directory,
                )
            except OSError as exc:
                raise RuntimeError(
                    f"Could not start MetaTrader terminal: {terminal_path}"
                ) from exc
            return process.wait() == 0
