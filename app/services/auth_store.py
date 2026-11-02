import hashlib
import hmac
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.services.database import (
    Database,
    mt5_credentials,
    users,
    web_sessions,
)


SESSION_DURATION = timedelta(hours=12)
VERIFICATION_DURATION = timedelta(hours=24)
PASSWORD_SCRYPT_N = 2**14


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=8,
        p=1,
        dklen=64,
    )
    return f"scrypt${PASSWORD_SCRYPT_N}${salt.hex()}${derived.hex()}"


def _password_matches(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n_value, salt_hex, expected_hex = stored_hash.split("$", 3)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_value),
            r=8,
            p=1,
            dklen=64,
        )
        return hmac.compare_digest(derived.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def _fernet() -> Fernet:
    key = os.getenv("MT5_CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("MT5_CREDENTIAL_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(
            "MT5_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc


def has_valid_credential_encryption_key() -> bool:
    try:
        _fernet()
    except RuntimeError:
        return False
    return True


class AuthStore:
    def __init__(self, database: Database):
        self.engine = database.engine

    def create_user(
        self, email: str, password: str, verification_token: str
    ) -> str | None:
        now = _utc_now()
        user_id = uuid4().hex
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(users).values(
                        user_id=user_id,
                        email=email,
                        password_hash=_password_hash(password),
                        email_verified=0,
                        verification_token_hash=_token_digest(verification_token),
                        verification_expires_at=_timestamp(
                            now + VERIFICATION_DURATION
                        ),
                        created_at=_timestamp(now),
                    )
                )
        except IntegrityError:
            with self.engine.connect() as connection:
                existing_user = connection.execute(
                    select(users.c.user_id).where(users.c.email == email)
                ).first()
            if existing_user is None:
                raise
            return None
        return user_id

    def delete_unverified_user(self, user_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(users).where(users.c.user_id == user_id))

    def authenticate(self, email: str, password: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(users).where(users.c.email == email)
            ).mappings().first()
        if row is None or not _password_matches(password, row["password_hash"]):
            return None
        return dict(row)

    def verify_email(self, token: str) -> bool:
        now = _utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(users)
                .where(
                    users.c.verification_token_hash == _token_digest(token),
                    users.c.email_verified == 0,
                    users.c.verification_expires_at > _timestamp(now),
                )
                .values(
                    email_verified=1,
                    verification_token_hash=None,
                    verification_expires_at=None,
                )
            )
        return result.rowcount == 1

    def create_session(self, user_id: str) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        now = _utc_now()
        expires_at = now + SESSION_DURATION
        with self.engine.begin() as connection:
            connection.execute(
                insert(web_sessions).values(
                    token_hash=_token_digest(token),
                    user_id=user_id,
                    expires_at=_timestamp(expires_at),
                    created_at=_timestamp(now),
                )
            )
        return token, expires_at

    def get_session_user(self, token: str) -> dict | None:
        now = _timestamp(_utc_now())
        with self.engine.connect() as connection:
            row = connection.execute(
                select(users.c.user_id, users.c.email)
                .select_from(
                    web_sessions.join(
                        users, web_sessions.c.user_id == users.c.user_id
                    )
                )
                .where(
                    web_sessions.c.token_hash == _token_digest(token),
                    web_sessions.c.expires_at > now,
                    users.c.email_verified == 1,
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    def delete_session(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(web_sessions).where(
                    web_sessions.c.token_hash == _token_digest(token)
                )
            )

    def save_credentials(
        self, user_id: str, login: str, password: str, server: str
    ) -> None:
        encrypted_password = _fernet().encrypt(password.encode("utf-8")).decode(
            "ascii"
        )
        with self.engine.begin() as connection:
            connection.execute(
                delete(mt5_credentials).where(
                    mt5_credentials.c.user_id == user_id
                )
            )
            connection.execute(
                insert(mt5_credentials).values(
                    user_id=user_id,
                    login=login,
                    password_encrypted=encrypted_password,
                    server=server,
                    updated_at=_timestamp(_utc_now()),
                )
            )

    def get_credentials(self, user_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(mt5_credentials).where(
                    mt5_credentials.c.user_id == user_id
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            password = _fernet().decrypt(
                row["password_encrypted"].encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Could not decrypt the saved MT5 credentials. Check the "
                "credential encryption key."
            ) from exc
        return {
            "login": row["login"],
            "password": password,
            "server": row["server"],
        }

    def has_credentials(self, user_id: str) -> bool:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(mt5_credentials.c.user_id).where(
                    mt5_credentials.c.user_id == user_id
                )
            ).first()
        return row is not None

    def credential_server(self, user_id: str) -> str | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(mt5_credentials.c.server).where(
                    mt5_credentials.c.user_id == user_id
                )
            ).first()
        return row[0] if row is not None else None


def send_verification_email(email: str, token: str) -> None:
    host = os.getenv("MT5_SMTP_HOST", "")
    sender = os.getenv("MT5_SMTP_FROM", "")
    base_url = os.getenv("MT5_PUBLIC_BASE_URL", "").rstrip("/")
    if not host or not sender or not base_url.startswith("https://"):
        raise RuntimeError(
            "Email verification requires SMTP host/from and an HTTPS public base URL."
        )

    message = EmailMessage()
    message["Subject"] = "Verify your MT5 AI Optimizer account"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "Verify your account within 24 hours by opening this link:\n\n"
        f"{base_url}/ui?verify={token}\n"
    )
    with smtplib.SMTP_SSL(
        host,
        int(os.getenv("MT5_SMTP_PORT", "465")),
        timeout=15,
    ) as smtp:
        username = os.getenv("MT5_SMTP_USERNAME", "")
        password = os.getenv("MT5_SMTP_PASSWORD", "")
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
