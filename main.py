import os
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.services.ai_engine.optimizer import AIOptimizer


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
UPLOADS_DIR = WORKSPACE_DIR / "inputs"
MAX_SET_FILE_SIZE = 10 * 1024 * 1024

load_dotenv(PROJECT_ROOT / ".env")
app = FastAPI(title="MT5 AI Optimizer API")


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


@app.get("/")
def read_root():
    return {
        "message": "MT5 AI Optimizer API",
        "upload_endpoint": "/optimize",
        "documentation": "/docs",
    }


@app.post("/optimize")
def optimize_uploaded_set(
    file: UploadFile = File(...),
    optimization_file: UploadFile | None = File(None),
    expert_name: str = Form(os.getenv("MT5_EXPERT_NAME", "")),
    symbol: str = Form("EURUSD"),
    period: str = Form("H1"),
    n_trials: int = Form(10, ge=1, le=1000),
    from_date: str = Form("2023.01.01"),
    to_date: str = Form("2023.12.31"),
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

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    set_file_path = UPLOADS_DIR / f"upload_{uuid.uuid4().hex}.set"
    set_file_path.write_bytes(content)
    if optimization_file is not None:
        optimization_set_file_path = (
            UPLOADS_DIR / f"optimization_{uuid.uuid4().hex}.set"
        )
        optimization_set_file_path.write_bytes(optimization_content)

    optimizer = AIOptimizer(
        terminal_path=str(terminal_path),
        base_workspace=str(WORKSPACE_DIR),
        account_info={
            "login": account_info["MT5_LOGIN"],
            "password": account_info["MT5_PASSWORD"],
            "server": account_info["MT5_SERVER"],
        },
    )
    try:
        optimizer.setup_task(
            str(set_file_path),
            expert_name,
            optimization_set_file_path=(
                str(optimization_set_file_path)
                if optimization_set_file_path is not None
                else None
            ),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if optimization_set_file_path is not None:
            optimization_set_file_path.unlink(missing_ok=True)

    result = optimizer.run(
        n_trials=n_trials,
        symbol=symbol,
        period=period,
        from_date=from_date,
        to_date=to_date,
    )
    return {
        "best_value": result["best_value"],
        "best_params": result["best_params"],
        "best_set_file": Path(result["best_set_file"]).name,
        "download_url": f"/results/{Path(result['best_set_file']).name}",
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


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("MT5_API_HOST", "127.0.0.1"),
        port=int(os.getenv("MT5_API_PORT", "8000")),
        reload=False,
    )
