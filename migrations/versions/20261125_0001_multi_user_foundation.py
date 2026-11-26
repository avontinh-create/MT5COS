"""Create multi-user tables and upgrade the existing jobs store.

Revision ID: 20261125_0001
Revises:
Create Date: 2026-11-25
"""

from alembic import op
from sqlalchemy import inspect, text

from app.services.database import metadata


revision = "20261125_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            table.create(bind, checkfirst=True)

    user_columns = {
        column["name"] for column in inspect(bind).get_columns("users")
    }
    if "password_reset_token_hash" not in user_columns:
        op.add_column(
            "users",
            metadata.tables["users"].c.password_reset_token_hash.copy(),
        )
    if "password_reset_expires_at" not in user_columns:
        op.add_column(
            "users",
            metadata.tables["users"].c.password_reset_expires_at.copy(),
        )

    job_columns = {
        column["name"]
        for column in inspect(bind).get_columns("optimization_jobs")
    }
    if "user_id" not in job_columns:
        op.add_column(
            "optimization_jobs",
            metadata.tables["optimization_jobs"].c.user_id.copy(),
        )
    bind.execute(
        text(
            "UPDATE optimization_jobs SET user_id = '__legacy__' "
            "WHERE user_id IS NULL"
        )
    )

    mutex_table = metadata.tables["database_mutex"]
    if "database_mutex" not in existing_tables:
        mutex_table.create(bind, checkfirst=True)
    if bind.dialect.name == "postgresql":
        bind.execute(
            text(
                "INSERT INTO database_mutex (lock_id, revision) VALUES (1, 0) "
                "ON CONFLICT (lock_id) DO NOTHING"
            )
        )
    else:
        bind.execute(
            text(
                "INSERT OR IGNORE INTO database_mutex (lock_id, revision) "
                "VALUES (1, 0)"
            )
        )


def downgrade() -> None:
    raise RuntimeError(
        "This data-preserving initial migration cannot be downgraded safely. "
        "Restore a verified database backup instead."
    )
