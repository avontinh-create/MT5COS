import argparse
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from alembic.runtime.migration import MigrationContext
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


EXPECTED_SCHEMA_REVISION = "20261125_0001"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def _enabled(environment: dict[str, str], name: str) -> bool:
    return environment.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _database_status(database_url: str) -> tuple[str, str]:
    try:
        url = make_url(database_url)
    except (ValueError, SQLAlchemyError):
        return "FAIL", "DATABASE_URL is invalid."
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        return "FAIL", "Production deployment requires PostgreSQL."

    engine = None
    try:
        engine = create_engine(
            url.set(drivername="postgresql+psycopg"),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
    except (ModuleNotFoundError, SQLAlchemyError, ValueError):
        return "FAIL", "PostgreSQL connection failed; check the URL, driver and access."
    finally:
        if engine is not None:
            engine.dispose()

    if revision != EXPECTED_SCHEMA_REVISION:
        return (
            "FAIL",
            "PostgreSQL is reachable but its schema migration is missing or outdated.",
        )
    return "PASS", "PostgreSQL connection and schema revision are ready."


def _smtp_status(environment: dict[str, str]) -> tuple[str, str]:
    host = environment.get("MT5_SMTP_HOST", "").strip()
    sender = environment.get("MT5_SMTP_FROM", "").strip()
    username = environment.get("MT5_SMTP_USERNAME", "")
    password = environment.get("MT5_SMTP_PASSWORD", "")
    try:
        port = int(environment.get("MT5_SMTP_PORT", "465"))
    except ValueError:
        return "FAIL", "MT5_SMTP_PORT must be a number from 1 to 65535."
    if not 1 <= port <= 65535:
        return "FAIL", "MT5_SMTP_PORT must be a number from 1 to 65535."
    if not host or not sender:
        return "FAIL", "Set MT5_SMTP_HOST and MT5_SMTP_FROM."
    if bool(username) != bool(password):
        return "FAIL", "Set both SMTP username and password, or leave both empty."
    return "PASS", f"SMTP settings are present (port {port}); network login is not tested."


def _public_url_status(value: str) -> tuple[str, str]:
    try:
        parsed = urlparse(value)
    except ValueError:
        return "FAIL", "MT5_PUBLIC_BASE_URL must be a real HTTPS origin without credentials."
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() in {"localhost", "your-domain.example"}
        or parsed.username
        or parsed.password
    ):
        return "FAIL", "MT5_PUBLIC_BASE_URL must be a real HTTPS origin without credentials."
    return "PASS", "Public base URL uses HTTPS."


def _encryption_key_status(value: str) -> tuple[str, str]:
    try:
        Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return "FAIL", "MT5_CREDENTIAL_ENCRYPTION_KEY is missing or invalid."
    return "PASS", "MT5 credential encryption key is valid."


def _mt5_files_status(environment: dict[str, str], project_root: Path) -> tuple[str, str]:
    terminal_value = environment.get("MT5_TERMINAL_PATH", "").strip()
    expert_name = environment.get("MT5_EXPERT_NAME", "").strip()
    if not terminal_value or not expert_name:
        return "FAIL", "Set MT5_TERMINAL_PATH and MT5_EXPERT_NAME."

    terminal = Path(terminal_value)
    if not terminal.is_absolute():
        terminal = project_root / terminal
    if not terminal.is_file():
        return "FAIL", "The configured MT5 terminal executable was not found."

    experts_directory = terminal.parent / "MQL5" / "Experts"
    expert = (experts_directory / expert_name).resolve()
    if expert.parent != experts_directory.resolve() or not expert.is_file():
        return "FAIL", "The configured EA file was not found under MQL5\\Experts."
    return "PASS", "MT5 terminal and EA files were found."


def check_deployment(
    environment: dict[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
    database_probe: Callable[[str], tuple[str, str]] = _database_status,
) -> list[CheckResult]:
    env = dict(os.environ if environment is None else environment)
    results: list[CheckResult] = []

    database_url = env.get("DATABASE_URL", "")
    database_result = (
        database_probe(database_url)
        if database_url
        else ("FAIL", "DATABASE_URL is not set.")
    )
    results.append(
        CheckResult("PostgreSQL", *database_result)
    )
    results.append(
        CheckResult(
            "HTTPS URL",
            *_public_url_status(env.get("MT5_PUBLIC_BASE_URL", "")),
        )
    )
    results.append(CheckResult("SMTP", *_smtp_status(env)))
    results.append(
        CheckResult(
            "Credential encryption",
            *_encryption_key_status(
                env.get("MT5_CREDENTIAL_ENCRYPTION_KEY", "")
            ),
        )
    )
    results.append(CheckResult("MT5 files", *_mt5_files_status(env, project_root)))

    api_host = env.get("MT5_API_HOST", "127.0.0.1").strip()
    try:
        is_loopback = ipaddress.ip_address(api_host).is_loopback
    except ValueError:
        is_loopback = api_host.lower() == "localhost"
    results.append(
        CheckResult(
            "API binding",
            "PASS" if is_loopback else "FAIL",
            (
                "API listens on loopback behind the reverse proxy."
                if is_loopback
                else "Set MT5_API_HOST to 127.0.0.1 or ::1."
            ),
        )
    )

    flags_ready = (
        _enabled(env, "MT5_EMAIL_VERIFICATION_ENABLED")
        and _enabled(env, "MT5_ACCOUNT_RECOVERY_ENABLED")
        and _enabled(env, "MT5_ACCOUNT_RECOVERY_READY")
        and _enabled(env, "MT5_COOKIE_SECURE")
        and not _enabled(env, "MT5_LEGACY_BASIC_AUTH_ENABLED")
        and not _enabled(env, "MT5_DRY_RUN")
    )
    results.append(
        CheckResult(
            "Account security",
            "PASS" if flags_ready else "FAIL",
            (
                "Email verification, recovery, secure cookies and production auth flags are ready."
                if flags_ready
                else (
                    "Enable email verification, account recovery, recovery support and "
                    "secure cookies; disable legacy Basic Auth and dry-run mode."
                )
            ),
        )
    )

    if _enabled(env, "MT5_PUBLIC_SIGNUP_ENABLED"):
        results.append(
            CheckResult("Public registration", "PASS", "Public registration is enabled.")
        )
    else:
        results.append(
            CheckResult(
                "Public registration",
                "INFO",
                "Registration remains disabled until you choose to enable it.",
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check production deployment configuration without printing secrets."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Environment file to load (default: project .env).",
    )
    arguments = parser.parse_args()
    if not arguments.env_file.is_file():
        print(f"FAIL  Environment file: not found at {arguments.env_file}")
        return 1

    load_dotenv(arguments.env_file, override=False)
    results = check_deployment()
    for result in results:
        print(f"{result.status:4}  {result.name}: {result.detail}")

    ready = all(result.status != "FAIL" for result in results)
    print(
        "READY: production configuration checks passed."
        if ready
        else "NOT READY: resolve the failed checks before enabling public registration."
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
