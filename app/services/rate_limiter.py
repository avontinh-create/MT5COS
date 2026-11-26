import math
import time

from sqlalchemy import delete, func, insert, select, update

from app.services.database import database_mutex, rate_limit_events


class SharedRateLimiter:
    def __init__(
        self,
        engine,
        *,
        scope: str,
        limit: int,
        window_seconds: int,
    ):
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate limit and window must be greater than zero.")
        if not scope or "|" in scope:
            raise ValueError("Rate-limit scope must be a non-empty label.")
        self.engine = engine
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds

    def acquire(self, client_key: str) -> int | None:
        bucket = f"{self.scope}|{client_key}"
        now = time.time()
        cutoff = now - self.window_seconds

        with self.engine.begin() as connection:
            connection.execute(
                update(database_mutex)
                .where(database_mutex.c.lock_id == 1)
                .values(revision=database_mutex.c.revision + 1)
            )
            connection.execute(
                delete(rate_limit_events).where(
                    rate_limit_events.c.bucket == bucket,
                    rate_limit_events.c.occurred_at <= cutoff,
                )
            )
            events = connection.execute(
                select(
                    func.count().label("count"),
                    func.min(rate_limit_events.c.occurred_at).label("oldest"),
                ).where(rate_limit_events.c.bucket == bucket)
            ).mappings().one()
            if events["count"] >= self.limit:
                return max(
                    1,
                    math.ceil(
                        events["oldest"] + self.window_seconds - now
                    ),
                )
            connection.execute(
                insert(rate_limit_events).values(
                    bucket=bucket,
                    occurred_at=now,
                )
            )
        return None
