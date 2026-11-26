from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool, StaticPool


metadata = MetaData()
EXPECTED_SCHEMA_REVISION = "20261125_0001"

users = Table(
    "users",
    metadata,
    Column("user_id", String(32), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("email_verified", Integer, nullable=False, server_default="0"),
    Column("verification_token_hash", String(64)),
    Column("verification_expires_at", String(40)),
    Column("password_reset_token_hash", String(64)),
    Column("password_reset_expires_at", String(40)),
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

database_mutex = Table(
    "database_mutex",
    metadata,
    Column("lock_id", Integer, primary_key=True),
    Column("revision", Integer, nullable=False, server_default="0"),
)

rate_limit_events = Table(
    "rate_limit_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("bucket", String(320), nullable=False),
    Column("occurred_at", Float, nullable=False),
)
Index(
    "ix_rate_limit_events_bucket_time",
    rate_limit_events.c.bucket,
    rate_limit_events.c.occurred_at,
)


class Database:
    def __init__(
        self,
        database_url: str | Path,
        *,
        initialize_schema: bool = True,
    ):
        if isinstance(database_url, Path):
            path = database_url.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = URL.create("sqlite", database=str(path))
        else:
            url = (
                database_url.replace(
                    "postgresql://", "postgresql+psycopg://", 1
                )
                if database_url.startswith("postgresql://")
                else database_url
            )
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
        if initialize_schema:
            metadata.create_all(self.engine)
            self._initialize_database_mutex()
            self._add_user_recovery_columns()
            self._add_legacy_job_owner_column()
        else:
            self._verify_schema_revision()

    def _verify_schema_revision(self) -> None:
        inspector = inspect(self.engine)
        if "alembic_version" not in inspector.get_table_names():
            raise RuntimeError(
                "The PostgreSQL schema has not been migrated. Run "
                "'alembic upgrade head' before starting the API."
            )
        with self.engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        if revision != EXPECTED_SCHEMA_REVISION:
            raise RuntimeError(
                "The PostgreSQL schema is not at the expected migration "
                f"revision {EXPECTED_SCHEMA_REVISION!r}; found {revision!r}. "
                "Run 'alembic upgrade head' before starting the API."
            )

    def _initialize_database_mutex(self) -> None:
        dialect = self.engine.dialect.name
        statement = (
            "INSERT INTO database_mutex (lock_id, revision) VALUES (1, 0) "
            "ON CONFLICT (lock_id) DO NOTHING"
            if dialect == "postgresql"
            else "INSERT OR IGNORE INTO database_mutex (lock_id, revision) VALUES (1, 0)"
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql(statement)

    def _add_user_recovery_columns(self) -> None:
        columns = {
            column["name"] for column in inspect(self.engine).get_columns("users")
        }
        statements = {
            "password_reset_token_hash": (
                "ALTER TABLE users ADD COLUMN password_reset_token_hash VARCHAR(64)"
            ),
            "password_reset_expires_at": (
                "ALTER TABLE users ADD COLUMN password_reset_expires_at VARCHAR(40)"
            ),
        }
        with self.engine.begin() as connection:
            for name, statement in statements.items():
                if name not in columns:
                    connection.exec_driver_sql(statement)

    def _add_legacy_job_owner_column(self) -> None:
        columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("optimization_jobs")
        }
        with self.engine.begin() as connection:
            if "user_id" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE optimization_jobs ADD COLUMN user_id VARCHAR(32)"
                )
            connection.exec_driver_sql(
                "UPDATE optimization_jobs SET user_id = '__legacy__' "
                "WHERE user_id IS NULL"
            )
