import base64
import binascii
import hmac
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.ai_engine.job_manager import JobStore, OptimizationJobManager
from app.services.ai_engine.optimizer import AIOptimizer


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
JOBS_DIR = WORKSPACE_DIR / "jobs"
JOB_DATABASE_PATH = WORKSPACE_DIR / "jobs.sqlite3"
MAX_SET_FILE_SIZE = 10 * 1024 * 1024

load_dotenv(PROJECT_ROOT / ".env")
app = FastAPI(title="MT5 AI Optimizer API")
job_manager = OptimizationJobManager(
    JobStore(JOB_DATABASE_PATH),
    JOBS_DIR,
    lambda config, progress: _execute_optimization_job(config, progress),
)


@app.on_event("startup")
def start_optimization_jobs():
    job_manager.start()


@app.on_event("shutdown")
def stop_optimization_jobs():
    job_manager.stop()


@app.middleware("http")
async def require_web_auth(request: Request, call_next):
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
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MT5 AI Optimizer"'},
        )

    return await call_next(request)


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


def _execute_optimization_job(config: dict, progress_callback) -> dict:
    required_account_vars = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    account_info = {name: os.getenv(name) for name in required_account_vars}
    missing_account_vars = [
        name for name, value in account_info.items() if not value
    ]
    if missing_account_vars:
        raise RuntimeError(
            "Missing MT5 account settings in .env: "
            + ", ".join(missing_account_vars)
        )

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
    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(WORKSPACE_DIR),
        account_info={
            "login": account_info["MT5_LOGIN"],
            "password": account_info["MT5_PASSWORD"],
            "server": account_info["MT5_SERVER"],
        },
    )
    parsed_set = optimizer.builder.parse_set_file(str(set_file_path))
    available_symbols = None
    if parsed_set.get("CurrencyList") is not None:
        available_symbols = optimizer.runner.get_available_symbols(
            account_info["MT5_LOGIN"],
            account_info["MT5_PASSWORD"],
            account_info["MT5_SERVER"],
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


@app.post("/optimize", status_code=202)
def optimize_uploaded_set(
    file: UploadFile = File(...),
    optimization_file: UploadFile | None = File(None),
    expert_name: str = Form(os.getenv("MT5_EXPERT_NAME", "")),
    symbol: str = Form("EURUSD"),
    period: str = Form("H1"),
    n_trials: int = Form(10, ge=1, le=1000),
    from_date: str = Form("2023.01.01"),
    to_date: str = Form("2023.12.31"),
    deposit: float = Form(10000, gt=0),
    currency: str = Form("USD", min_length=3, max_length=3),
    leverage: int = Form(100, ge=1, le=10000),
    model: int = Form(1, ge=0, le=4),
    execution_mode: int = Form(0, ge=0, le=1),
):
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
    required_account_vars = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    account_info = {name: os.getenv(name) for name in required_account_vars}
    missing_account_vars = [
        name for name, value in account_info.items() if not value
    ]
    if missing_account_vars:
        raise HTTPException(
            status_code=503,
            detail=(
                "Missing MT5 account settings in .env: "
                + ", ".join(missing_account_vars)
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
    _validate_expert_file(terminal_path, expert_name)

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

        validation_builder = AIOptimizer(
            terminal_path=str(terminal_path),
            base_workspace=str(WORKSPACE_DIR),
            account_info={
                "login": "",
                "password": "",
                "server": "",
            },
        ).builder
        validation_builder.parse_set_file(str(set_file_path))
        if optimization_set_file_path is not None:
            validation_builder.parse_set_file(str(optimization_set_file_path))
        config = {
            "job_id": job_id,
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
        job_manager.submit(job_id, config, n_trials)
    except (FileNotFoundError, ValueError) as exc:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@app.get("/jobs/{job_id}")
def get_optimization_job(job_id: str):
    job = job_manager.get(job_id)
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
        "error": job["error"],
        "result": job["result"],
    }


@app.get("/results/{filename}")
def download_best_set_file(filename: str):
    if (
        Path(filename).name != filename
        or not filename.startswith("trial_")
        or not filename.endswith("_BEST_RESULT.set")
    ):
        raise HTTPException(status_code=404, detail="Result file not found.")

    result_path = WORKSPACE_DIR / "temp_configs" / filename
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="Result file not found.")
    return FileResponse(result_path, filename=filename, media_type="application/octet-stream")


@app.get("/reports/{filename}")
def download_excel_report(filename: str):
    if (
        Path(filename).name != filename
        or not filename.startswith("report_trial_")
        or not filename.endswith(".xlsx")
    ):
        raise HTTPException(status_code=404, detail="Excel report not found.")

    report_path = WORKSPACE_DIR / "reports" / filename
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
