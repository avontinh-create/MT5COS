from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool, StaticPool


metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("user_id", String(32), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("email_verified", Integer, nullable=False, server_default="0"),
    Column("verification_token_hash", String(64)),
    Column("verification_expires_at", String(40)),
    Column("created_at", String(40), nullable=False),
)

web_sessions = Table(
    "web_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id"), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
)

mt5_credentials = Table(
    "mt5_credentials",
    metadata,
    Column(
        "user_id",
        String(32),
        ForeignKey("users.user_id"),
        primary_key=True,
    ),
    Column("login", String(32), nullable=False),
    Column("password_encrypted", Text, nullable=False),
    Column("server", String(128), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

optimization_jobs = Table(
    "optimization_jobs",
    metadata,
    Column("job_id", String(64), primary_key=True),
    Column("user_id", String(32)),
    Column("status", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("completed_trials", Integer, nullable=False, server_default="0"),
    Column("total_trials", Integer, nullable=False),
    Column("best_value", Float),
    Column("error", Text),
    Column("config_json", Text, nullable=False),
    Column("result_json", Text),
)


class Database:
    def __init__(self, database_url: str | Path):
        if isinstance(database_url, Path):
            path = database_url.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = URL.create("sqlite", database=str(path))
        else:
            url = database_url
            if "://" not in url:
                path = Path(url).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                url = URL.create("sqlite", database=str(path))

        connect_args = {"timeout": 30} if str(url).startswith("sqlite") else {}
        engine_options = {"pool_pre_ping": True, "connect_args": connect_args}
        if str(url) in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
            engine_options["connect_args"] = {"check_same_thread": False}
        elif str(url).startswith("sqlite"):
            engine_options["poolclass"] = NullPool
        self.engine: Engine = create_engine(url, **engine_options)
        metadata.create_all(self.engine)
        self._add_legacy_job_owner_column()

    def _add_legacy_job_owner_column(self) -> None:
        columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("optimization_jobs")
        }
        if "user_id" in columns:
            return
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE optimization_jobs ADD COLUMN user_id VARCHAR(32)"
            )
            connection.exec_driver_sql(
                "UPDATE optimization_jobs SET user_id = '__legacy__' "
                "WHERE user_id IS NULL"
            )
