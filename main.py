import base64
import binascii
import hmac
import ipaddress
import importlib.util
import logging
import os
import re
import secrets
import shutil
import smtplib
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.ai_engine.job_manager import (
    JobQueueFullError,
    JobStore,
    OptimizationJobManager,
)
from app.services.ai_engine.optimizer import AIOptimizer
from app.services.auth_store import (
    AuthStore,
    has_valid_credential_encryption_key,
    send_verification_email,
)
from app.services.database import Database
from app.services.mt5_engine.runner import MT5BacktestRunner


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
JOBS_DIR = WORKSPACE_DIR / "jobs"
JOB_DATABASE_PATH = WORKSPACE_DIR / "jobs.sqlite3"
MAX_SET_FILE_SIZE = 10 * 1024 * 1024
MAX_QUEUED_JOBS = 5
MAX_TRIALS_PER_JOB = 100
MAX_OPTIMIZATIONS_PER_MINUTE_PER_IP = 3
OPTIMIZATION_RATE_WINDOW_SECONDS = 60
LEGACY_USER_ID = "__legacy__"
SESSION_COOKIE_NAME = "mt5_session"
logger = logging.getLogger(__name__)


class OptimizationRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, client_ip: str) -> int | None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            expired_keys = []
            for key, timestamps in self._requests.items():
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if not timestamps:
                    expired_keys.append(key)
            for key in expired_keys:
                del self._requests[key]

            timestamps = self._requests.setdefault(client_ip, deque())
            if len(timestamps) >= self.limit:
                return max(1, int(timestamps[0] + self.window_seconds - now + 0.999))
            timestamps.append(now)
            return None

load_dotenv(PROJECT_ROOT / ".env")
app = FastAPI(title="MT5 AI Optimizer API")
optimization_rate_limiter = OptimizationRateLimiter(
    MAX_OPTIMIZATIONS_PER_MINUTE_PER_IP,
    OPTIMIZATION_RATE_WINDOW_SECONDS,
)
auth_rate_limiter = OptimizationRateLimiter(10, 15 * 60)
signup_rate_limiter = OptimizationRateLimiter(5, 60 * 60)
database = Database(os.getenv("DATABASE_URL", str(JOB_DATABASE_PATH)))
auth_store = AuthStore(database)
job_manager = OptimizationJobManager(
    JobStore(database),
    JOBS_DIR,
    lambda config, progress: _execute_job(config, progress),
    maximum_queued_jobs=MAX_QUEUED_JOBS,
)


@app.on_event("startup")
def start_optimization_jobs():
    job_manager.start()


@app.on_event("shutdown")
def stop_optimization_jobs():
    job_manager.stop()


@app.middleware("http")
async def require_web_auth(request: Request, call_next):
    if request.url.path in {
        "/",
        "/ui",
        "/auth/config",
        "/auth/register",
        "/auth/verify",
        "/auth/login",
    }:
        return await call_next(request)

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = auth_store.get_session_user(session_token)
        if user is not None:
            request.state.user_id = user["user_id"]
            request.state.user_email = user["email"]
            return await call_next(request)

    if _is_local_preview_request(request):
        request.state.user_id = LEGACY_USER_ID
        request.state.user_email = None
        return await call_next(request)

    if not _legacy_basic_auth_enabled():
        return JSONResponse(
            status_code=401,
            content={"detail": "Sign in to access this resource."},
        )

    username = os.getenv("MT5_WEB_USERNAME", "")
    password = os.getenv("MT5_WEB_PASSWORD", "")
    if (
        not username
        or username == "change-this-username"
        or len(password) < 16
        or password == "replace-with-a-long-random-password"
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": "Web authentication is not configured."},
        )

    authorization = request.headers.get("authorization", "")
    scheme, separator, encoded_credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not separator:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MT5 AI Optimizer"'},
        )

    try:
        decoded_credentials = base64.b64decode(
            encoded_credentials, validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        decoded_credentials = ""

    supplied_username, separator, supplied_password = decoded_credentials.partition(":")
    if (
        not separator
        or not hmac.compare_digest(supplied_username, username)
        or not hmac.compare_digest(supplied_password, password)
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "Sign in to access this resource."},
        )

    request.state.user_id = LEGACY_USER_ID
    request.state.user_email = None
    return await call_next(request)


class LoginPayload(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegisterPayload(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class VerifyPayload(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class MT5CredentialPayload(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=1, max_length=128)


def _environment_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_basic_auth_enabled() -> bool:
    return os.getenv("MT5_LEGACY_BASIC_AUTH_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _signup_disabled_reason() -> str | None:
    if not _environment_flag("MT5_PUBLIC_SIGNUP_ENABLED"):
        return "Public registration is disabled by the server."
    if _legacy_basic_auth_enabled():
        return "Disable legacy shared Basic authentication before public registration."
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        return "Public registration requires a PostgreSQL DATABASE_URL."
    if not _environment_flag("MT5_EMAIL_VERIFICATION_ENABLED"):
        return "Email verification must be enabled before public registration."
    if not _environment_flag("MT5_ACCOUNT_RECOVERY_READY"):
        return "Account recovery support must be ready before public registration."
    if not os.getenv("MT5_SMTP_HOST") or not os.getenv("MT5_SMTP_FROM"):
        return "SMTP host and sender must be configured for email verification."
    smtp_username = os.getenv("MT5_SMTP_USERNAME", "")
    smtp_password = os.getenv("MT5_SMTP_PASSWORD", "")
    if bool(smtp_username) != bool(smtp_password):
        return "Configure both SMTP username and password, or neither."
    if not os.getenv("MT5_PUBLIC_BASE_URL", "").startswith("https://"):
        return "Public registration requires an HTTPS MT5_PUBLIC_BASE_URL."
    if not _environment_flag("MT5_COOKIE_SECURE"):
        return "Secure session cookies must be enabled for public registration."
    if not has_valid_credential_encryption_key():
        return "A valid MT5 credential encryption key is required."
    return None


def _request_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user_id


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/auth/config")
def get_auth_config():
    return {
        "registration_enabled": _signup_disabled_reason() is None,
        "registration_message": _signup_disabled_reason(),
    }


@app.post("/auth/register", status_code=202)
def register_account(payload: RegisterPayload, request: Request):
    disabled_reason = _signup_disabled_reason()
    if disabled_reason is not None:
        raise HTTPException(status_code=503, detail=disabled_reason)
    retry_after = signup_rate_limiter.acquire(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many registration attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    email = payload.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    if len(payload.password.encode("utf-8")) > 1024:
        raise HTTPException(status_code=422, detail="Password is too long.")

    verification_token = secrets.token_urlsafe(32)
    user_id = auth_store.create_user(email, payload.password, verification_token)
    if user_id is not None:
        try:
            send_verification_email(email, verification_token)
        except (OSError, smtplib.SMTPException, RuntimeError, ValueError) as exc:
            auth_store.delete_unverified_user(user_id)
            logger.exception("Could not send account verification email")
            raise HTTPException(
                status_code=503,
                detail="Verification email could not be sent. Please try again later.",
            ) from exc
    return {
        "message": (
            "If this email can be registered, a verification link will be sent."
        )
    }


@app.post("/auth/verify")
def verify_account(payload: VerifyPayload):
    if not auth_store.verify_email(payload.token):
        raise HTTPException(
            status_code=400,
            detail="Verification link is invalid or has expired.",
        )
    return {"message": "Email verified. You can now sign in."}


@app.post("/auth/login")
def login_account(payload: LoginPayload, response: Response, request: Request):
    retry_after = auth_rate_limiter.acquire(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many sign-in attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    user = auth_store.authenticate(payload.email.strip().lower(), payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not user["email_verified"]:
        raise HTTPException(status_code=403, detail="Verify your email before signing in.")

    token, expires_at = auth_store.create_session(user["user_id"])
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds()),
        httponly=True,
        secure=_environment_flag("MT5_COOKIE_SECURE"),
        samesite="strict",
        path="/",
    )
    return {"user_id": user["user_id"], "email": user["email"]}


@app.get("/auth/me")
def current_account(request: Request):
    user_id = _request_user_id(request)
    if user_id == LEGACY_USER_ID:
        return {"user_id": user_id, "email": None, "legacy_auth": True}
    return {
        "user_id": user_id,
        "email": getattr(request.state, "user_email", None),
        "legacy_auth": False,
    }


@app.post("/auth/logout")
def logout_account(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth_store.delete_session(token)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=_environment_flag("MT5_COOKIE_SECURE"),
        samesite="strict",
        path="/",
    )
    return {"message": "Signed out."}


@app.put("/account/mt5")
def save_mt5_credentials(payload: MT5CredentialPayload, request: Request):
    user_id = _request_user_id(request)
    if user_id == LEGACY_USER_ID:
        raise HTTPException(
            status_code=409,
            detail="Sign in with a verified user account to save MT5 credentials.",
        )
    if not has_valid_credential_encryption_key():
        raise HTTPException(
            status_code=503,
            detail="MT5 credential encryption is not configured on this server.",
        )
    if not payload.login.isascii() or not payload.login.isdigit():
        raise HTTPException(status_code=422, detail="MT5 login must contain digits only.")
    if not payload.server.strip():
        raise HTTPException(status_code=422, detail="MT5 server is required.")
    auth_store.save_credentials(
        user_id,
        payload.login,
        payload.password,
        payload.server.strip(),
    )
    return {"saved": True}


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_preview_request(request: Request) -> bool:
    if os.getenv("MT5_ALLOW_LOCAL_NO_AUTH", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    client_host = request.client.host if request.client else ""
    requested_host = request.url.hostname or ""
    return (
        _is_loopback_host(client_host)
        and (
            requested_host.lower() == "localhost"
            or _is_loopback_host(requested_host)
        )
    )


def _configured_path(variable: str, default: Path) -> Path:
    configured = os.getenv(variable)
    path = Path(configured) if configured else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _validate_dates(from_date: str, to_date: str) -> tuple[str, str]:
    try:
        start = datetime.strptime(from_date, "%Y.%m.%d")
        end = datetime.strptime(to_date, "%Y.%m.%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Dates must use YYYY.MM.DD format."
        ) from exc
    if start > end:
        raise HTTPException(
            status_code=422, detail="from_date must be on or before to_date."
        )
    return from_date, to_date


def _validate_expert_file(terminal_path: Path, expert_name: str):
    relative_path = Path(expert_name.replace("/", os.sep))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HTTPException(
            status_code=400,
            detail="expert_name must be relative to the MQL5\\Experts folder.",
        )
    if terminal_path.suffix.lower() == ".lnk":
        return

    experts_dir = terminal_path.parent / "MQL5" / "Experts"
    expert_path = experts_dir / relative_path
    if not expert_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=(
                f"EA not found at {expert_path}. Copy the compiled .ex5 file into "
                "MQL5\\Experts and pass its path relative to that folder."
            ),
        )


def _configured_account() -> dict[str, str]:
    return {
        key: os.getenv(environment_name, "")
        for key, environment_name in (
            ("login", "MT5_LOGIN"),
            ("password", "MT5_PASSWORD"),
            ("server", "MT5_SERVER"),
        )
    }


def _account_for_job(user_id: str | None) -> dict[str, str]:
    if user_id == LEGACY_USER_ID or user_id is None:
        account = _configured_account()
    else:
        account = auth_store.get_credentials(user_id) or {}
    if not all(account.get(key) for key in ("login", "password", "server")):
        raise RuntimeError(
            "MT5 credentials are not configured for the account that owns this job."
        )
    return account


def _user_workspace(user_id: str | None) -> Path:
    if user_id in {None, LEGACY_USER_ID}:
        return WORKSPACE_DIR
    if not re.fullmatch(r"[a-f0-9]{32}", user_id):
        raise ValueError("Invalid user workspace identifier.")
    return WORKSPACE_DIR / "users" / user_id


def _execute_job(config: dict, progress_callback) -> dict:
    account = _account_for_job(config.get("user_id"))
    if config.get("job_type") == "connection_check":
        return MT5BacktestRunner(config["terminal_path"]).check_connection(
            account["login"], account["password"], account["server"]
        )
    return _execute_optimization_job(config, progress_callback, account)


def _execute_optimization_job(
    config: dict, progress_callback, account: dict[str, str]
) -> dict:
    terminal_path = _configured_path(
        "MT5_TERMINAL_PATH", PROJECT_ROOT / "mt5_portable" / "terminal64.exe"
    )
    if not terminal_path.is_file():
        raise FileNotFoundError(f"MT5 terminal was not found at {terminal_path}.")
    _validate_expert_file(terminal_path, config["expert_name"])

    job_directory = JOBS_DIR / config["job_id"]
    set_file_path = job_directory / "parameters.set"
    optimization_path = (
        job_directory / "optimization_ranges.set"
        if config["has_optimization_file"]
        else None
    )
    account_workspace = _user_workspace(config.get("user_id"))
    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(account_workspace),
        account_info={
            "login": account["login"],
            "password": account["password"],
            "server": account["server"],
        },
    )
    parsed_set = optimizer.builder.parse_set_file(str(set_file_path))
    available_symbols = None
    if parsed_set.get("CurrencyList") is not None:
        available_symbols = optimizer.runner.get_available_symbols(
            account["login"],
            account["password"],
            account["server"],
        )
    optimizer.setup_task(
        str(set_file_path),
        config["expert_name"],
        optimization_set_file_path=(
            str(optimization_path) if optimization_path is not None else None
        ),
        available_symbols=available_symbols,
    )
    result = optimizer.run(
        n_trials=config["n_trials"],
        symbol=config["symbol"],
        period=config["period"],
        from_date=config["from_date"],
        to_date=config["to_date"],
        deposit=config["deposit"],
        currency=config["currency"],
        leverage=config["leverage"],
        model=config["model"],
        execution_mode=config["execution_mode"],
        progress_callback=progress_callback,
    )
    best_set_name = Path(result["best_set_file"]).name
    return {
        "best_value": result["best_value"],
        "best_params": result["best_params"],
        "best_set_file": best_set_name,
        "download_url": f"/results/{best_set_name}",
        "excel_reports": [
            {
                "filename": filename,
                "download_url": f"/reports/{filename}",
            }
            for filename in result["excel_reports"]
        ],
    }


@app.get("/")
def read_root():
    return {
        "message": "MT5 AI Optimizer API",
        "interface": "/ui",
        "upload_endpoint": "/optimize",
        "documentation": "/docs",
    }


@app.get("/ui")
def optimizer_interface():
    return FileResponse(PROJECT_ROOT / "web" / "optimizer.html")


@app.get("/system/status")
def get_system_status(request: Request):
    user_id = _request_user_id(request)
    terminal_path = _configured_path(
        "MT5_TERMINAL_PATH", PROJECT_ROOT / "mt5_portable" / "terminal64.exe"
    )
    expert_name = os.getenv("MT5_EXPERT_NAME", "")
    expert_exists = False
    expert_status = "not_configured" if not expert_name else "not_checked"
    if expert_name and terminal_path.suffix.lower() != ".lnk":
        relative_expert_path = Path(expert_name.replace("/", os.sep))
        if not relative_expert_path.is_absolute() and ".." not in relative_expert_path.parts:
            expert_exists = (
                terminal_path.parent / "MQL5" / "Experts" / relative_expert_path
            ).is_file()
        expert_status = "available" if expert_exists else "missing"

    if user_id == LEGACY_USER_ID:
        account = _configured_account()
        account_configured = all(account.values())
        account_server = account["server"] if account_configured else None
        account_status = "configured" if account_configured else "missing"
    else:
        saved_credentials = auth_store.has_credentials(user_id)
        key_available = has_valid_credential_encryption_key()
        account_server = (
            auth_store.credential_server(user_id) if saved_credentials else None
        )
        account_configured = saved_credentials and key_available
        account_status = (
            "configured"
            if account_configured
            else (
                "encryption_key_missing"
                if saved_credentials
                else "missing"
            )
        )
    latest_job = job_manager.latest(user_id)
    latest_job_status = None
    if latest_job is not None:
        latest_job_status = {
            "job_id": latest_job["job_id"],
            "status": latest_job["status"],
            "updated_at": latest_job["updated_at"],
            "completed_trials": latest_job["completed_trials"],
            "total_trials": latest_job["total_trials"],
            "error": latest_job["error"],
            "terminal_build": (
                latest_job["result"].get("terminal_build")
                if latest_job["result"]
                else None
            ),
        }
    return {
        "application": {"status": "ready"},
        "mt5_package": {
            "status": (
                "ready" if importlib.util.find_spec("MetaTrader5") else "missing"
            )
        },
        "account": {
            "status": account_status,
            "server": account_server if account_configured else None,
        },
        "terminal": {
            "status": "available" if terminal_path.is_file() else "missing",
            "path": str(terminal_path),
        },
        "expert": {
            "status": expert_status,
            "name": expert_name or None,
        },
        "broker": {"status": "not_checked"},
        "jobs": job_manager.queue_status(),
        "latest_job": latest_job_status,
    }


@app.post("/connection-check", status_code=202)
def submit_connection_check(request: Request):
    user_id = _request_user_id(request)
    if user_id == LEGACY_USER_ID:
        account = _configured_account()
        missing_settings = [
            key for key, value in account.items() if not value
        ]
    else:
        if (
            auth_store.has_credentials(user_id)
            and not has_valid_credential_encryption_key()
        ):
            raise HTTPException(
                status_code=503,
                detail="MT5 credential encryption is not configured on this server.",
            )
        missing_settings = [] if auth_store.has_credentials(user_id) else ["account"]
    if missing_settings:
        raise HTTPException(
            status_code=409,
            detail=(
                "Save valid MT5 account credentials before checking the connection."
            ),
        )

    terminal_path = _configured_path(
        "MT5_TERMINAL_PATH", PROJECT_ROOT / "mt5_portable" / "terminal64.exe"
    )
    if not terminal_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"MT5 terminal was not found at {terminal_path}.",
        )

    job_id = uuid.uuid4().hex
    try:
        job_manager.submit(
            job_id,
            {
                "job_id": job_id,
                "job_type": "connection_check",
                "terminal_path": str(terminal_path),
                "user_id": user_id,
            },
            1,
            user_id,
        )
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@app.post("/optimize", status_code=202)
def optimize_uploaded_set(
    request: Request,
    file: UploadFile = File(...),
    optimization_file: UploadFile | None = File(None),
    expert_name: str = Form(os.getenv("MT5_EXPERT_NAME", "")),
    symbol: str = Form("EURUSD"),
    period: str = Form("H1"),
    n_trials: int = Form(10, ge=1, le=MAX_TRIALS_PER_JOB),
    from_date: str = Form("2023.01.01"),
    to_date: str = Form("2023.12.31"),
    deposit: float = Form(10000, gt=0),
    currency: str = Form("USD", min_length=3, max_length=3),
    leverage: int = Form(100, ge=1, le=10000),
    model: int = Form(1, ge=0, le=4),
    execution_mode: int = Form(0, ge=0, le=1),
):
    user_id = _request_user_id(request)
    if not file.filename or Path(file.filename).suffix.lower() != ".set":
        raise HTTPException(status_code=400, detail="Upload a .set parameter file.")

    content = file.file.read(MAX_SET_FILE_SIZE + 1)
    if len(content) > MAX_SET_FILE_SIZE:
        raise HTTPException(status_code=413, detail="The .set file exceeds 10 MB.")
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded .set file is empty.")

    optimization_set_file_path = None
    if optimization_file is not None:
        if (
            not optimization_file.filename
            or Path(optimization_file.filename).suffix.lower() != ".set"
        ):
            raise HTTPException(
                status_code=400,
                detail="Optimization ranges must be uploaded as a .set file.",
            )
        optimization_content = optimization_file.file.read(MAX_SET_FILE_SIZE + 1)
        if len(optimization_content) > MAX_SET_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail="The optimization range file exceeds 10 MB.",
            )
        if not optimization_content:
            raise HTTPException(
                status_code=400,
                detail="The optimization range file is empty.",
            )

    from_date, to_date = _validate_dates(from_date, to_date)
    if not currency.isascii() or not currency.isalpha():
        raise HTTPException(
            status_code=422, detail="Tester currency must be a three-letter code."
        )
    if model not in {0, 1, 2, 4}:
        raise HTTPException(
            status_code=422,
            detail="Select a financial tick model (Every tick, 1 minute OHLC, "
            "Open prices, or Real ticks).",
        )
    if execution_mode == 1 and model == 2:
        raise HTTPException(
            status_code=422,
            detail=(
                "MQL5 Random Delay cannot be used with Open prices only. "
                "Choose No Delay or another tick model."
            ),
        )
    if user_id == LEGACY_USER_ID:
        account_info = _configured_account()
        account_configured = all(account_info.values())
    else:
        account_configured = auth_store.has_credentials(user_id)
        if account_configured and not has_valid_credential_encryption_key():
            raise HTTPException(
                status_code=503,
                detail="MT5 credential encryption is not configured on this server.",
            )
    if not account_configured:
        raise HTTPException(
            status_code=409,
            detail="Save valid MT5 account credentials before submitting an optimization.",
        )

    terminal_path = _configured_path(
        "MT5_TERMINAL_PATH", PROJECT_ROOT / "mt5_portable" / "terminal64.exe"
    )
    if not terminal_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"MT5 terminal was not found at {terminal_path}.",
        )
    _validate_expert_file(terminal_path, expert_name)

    client_ip = request.client.host if request.client else "unknown"
    retry_after = optimization_rate_limiter.acquire(client_ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many optimization requests from this IP. "
                f"Try again in {retry_after} seconds."
            ),
            headers={"Retry-After": str(retry_after)},
        )

    job_id = uuid.uuid4().hex
    job_directory = JOBS_DIR / job_id
    set_file_path = job_directory / "parameters.set"
    optimization_set_file_path = (
        job_directory / "optimization_ranges.set"
        if optimization_file is not None
        else None
    )
    has_optimization_file = optimization_file is not None
    try:
        job_directory.mkdir(parents=True, exist_ok=False)
        set_file_path.write_bytes(content)
        if optimization_set_file_path is not None:
            optimization_set_file_path.write_bytes(optimization_content)

        validation_optimizer = AIOptimizer(
            terminal_path=str(terminal_path),
            base_workspace=str(_user_workspace(user_id)),
            account_info={
                "login": "",
                "password": "",
                "server": "",
            },
        )
        try:
            validation_optimizer.setup_task(
                str(set_file_path),
                expert_name,
                optimization_set_file_path=(
                    str(optimization_set_file_path)
                    if optimization_set_file_path is not None
                    else None
                ),
            )
        except ValueError as exc:
            if "no parameters enabled for optimization" in str(exc):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "File tham số EA chưa bật tham số nào để tối ưu. "
                        "Hãy tải file optimization_ranges.set (hoặc file phạm vi "
                        ".set tương ứng) vào ô "
                        "'File phạm vi tối ưu', hoặc bật ít nhất một tham số "
                        "số bằng cờ Y trong file .set."
                    ),
                ) from exc
            raise
        config = {
            "job_id": job_id,
            "user_id": user_id,
            "has_optimization_file": has_optimization_file,
            "expert_name": expert_name,
            "symbol": symbol,
            "period": period,
            "n_trials": n_trials,
            "from_date": from_date,
            "to_date": to_date,
            "deposit": deposit,
            "currency": currency.upper(),
            "leverage": leverage,
            "model": model,
            "execution_mode": execution_mode,
        }
        job_manager.submit(job_id, config, n_trials, user_id)
    except (FileNotFoundError, ValueError) as exc:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobQueueFullError as exc:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@app.get("/jobs/{job_id}")
def get_optimization_job(job_id: str, request: Request):
    user_id = _request_user_id(request)
    job = job_manager.get(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Optimization job not found.")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_trials": job["completed_trials"],
        "total_trials": job["total_trials"],
        "best_value": job["best_value"],
        **job_manager.queue_status(job_id),
        "error": job["error"],
        "result": job["result"],
    }


@app.get("/results/{filename}")
def download_best_set_file(filename: str, request: Request):
    user_id = _request_user_id(request)
    if (
        Path(filename).name != filename
        or not filename.startswith("trial_")
        or not filename.endswith("_BEST_RESULT.set")
    ):
        raise HTTPException(status_code=404, detail="Result file not found.")

    if not job_manager.store.owns_result_file(user_id, filename):
        raise HTTPException(status_code=404, detail="Result file not found.")
    result_path = _user_workspace(user_id) / "temp_configs" / filename
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="Result file not found.")
    return FileResponse(result_path, filename=filename, media_type="application/octet-stream")


@app.get("/reports/{filename}")
def download_excel_report(filename: str, request: Request):
    user_id = _request_user_id(request)
    if (
        Path(filename).name != filename
        or not filename.startswith("report_trial_")
        or not filename.endswith(".xlsx")
    ):
        raise HTTPException(status_code=404, detail="Excel report not found.")

    if not job_manager.store.owns_result_file(user_id, filename, is_report=True):
        raise HTTPException(status_code=404, detail="Excel report not found.")
    report_path = _user_workspace(user_id) / "reports" / filename
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Excel report not found.")
    return FileResponse(
        report_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("MT5_API_HOST", "127.0.0.1"),
        port=int(os.getenv("MT5_API_PORT", "8000")),
        reload=False,
    )
