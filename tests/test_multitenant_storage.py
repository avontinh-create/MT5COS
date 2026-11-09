import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

import main
from app.services.ai_engine.job_manager import JobStore, OptimizationJobManager
from app.services.auth_store import AuthStore
from app.services.database import Database


class MultiTenantStorageTests(unittest.TestCase):
    def test_postgres_url_uses_installed_psycopg_driver(self):
        with (
            patch("app.services.database.metadata.create_all"),
            patch.object(Database, "_add_user_recovery_columns"),
            patch.object(Database, "_add_legacy_job_owner_column"),
        ):
            database = Database(
                "postgresql://optimizer:secret@localhost/optimizer"
            )
        try:
            self.assertEqual(database.engine.url.drivername, "postgresql+psycopg")
        finally:
            database.engine.dispose()

    def test_existing_database_is_upgraded_for_recovery_and_legacy_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE users (
                    user_id VARCHAR(32) PRIMARY KEY,
                    email VARCHAR(320) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    email_verified INTEGER NOT NULL,
                    verification_token_hash VARCHAR(64),
                    verification_expires_at VARCHAR(40),
                    created_at VARCHAR(40) NOT NULL
                );
                CREATE TABLE optimization_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_trials INTEGER NOT NULL DEFAULT 0,
                    total_trials INTEGER NOT NULL,
                    best_value REAL,
                    error TEXT,
                    config_json TEXT NOT NULL,
                    result_json TEXT
                );
                INSERT INTO optimization_jobs
                    (job_id, status, created_at, updated_at, total_trials, config_json)
                VALUES ('old-job', 'completed', 'now', 'now', 1, '{}');
                """
            )
            connection.close()

            database = Database(database_path)
            try:
                user_columns = {
                    column["name"]
                    for column in inspect(database.engine).get_columns("users")
                }
                with database.engine.connect() as connection:
                    legacy_owner = connection.execute(
                        text(
                            "SELECT user_id FROM optimization_jobs "
                            "WHERE job_id = 'old-job'"
                        )
                    ).scalar_one()

                self.assertIn("password_reset_token_hash", user_columns)
                self.assertIn("password_reset_expires_at", user_columns)
                self.assertEqual(legacy_owner, main.LEGACY_USER_ID)
            finally:
                database.engine.dispose()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "accounts.sqlite3")
        self.auth_store = AuthStore(self.database)
        self.job_store = JobStore(self.database)
        self.user_a = self._create_user("a@example.test")
        self.user_b = self._create_user("b@example.test")

    def _create_user(self, email):
        token = f"verify-{email}-token"
        user_id = self.auth_store.create_user(
            email, "correct-horse-battery-1", token
        )
        self.assertIsNotNone(user_id)
        self.assertTrue(self.auth_store.verify_email(token))
        return user_id

    def test_job_status_and_result_files_are_scoped_to_owner(self):
        self.job_store.create(
            "job-a",
            {"user_id": self.user_a},
            1,
            self.user_a,
        )
        self.job_store.update(
            "job-a",
            status="completed",
            result={
                "best_set_file": "trial_a_BEST_RESULT.set",
                "excel_reports": [{"filename": "report_trial_a.xlsx"}],
            },
        )
        self.job_store.create(
            "job-b",
            {"user_id": self.user_b},
            1,
            self.user_b,
        )

        self.assertIsNotNone(self.job_store.get("job-a", self.user_a))
        self.assertIsNone(self.job_store.get("job-a", self.user_b))
        self.assertEqual(self.job_store.latest(self.user_b)["job_id"], "job-b")
        self.assertEqual(self.job_store.latest(self.user_a)["job_id"], "job-a")
        self.assertTrue(
            self.job_store.owns_result_file(
                self.user_a, "trial_a_BEST_RESULT.set"
            )
        )
        self.assertFalse(
            self.job_store.owns_result_file(
                self.user_b, "trial_a_BEST_RESULT.set"
            )
        )
        self.assertTrue(
            self.job_store.owns_result_file(
                self.user_a, "report_trial_a.xlsx", is_report=True
            )
        )
        self.assertFalse(
            self.job_store.owns_result_file(
                self.user_b, "report_trial_a.xlsx", is_report=True
            )
        )

    def test_mt5_password_is_encrypted_in_database(self):
        key = Fernet.generate_key().decode("ascii")
        with patch.dict("os.environ", {"MT5_CREDENTIAL_ENCRYPTION_KEY": key}):
            self.auth_store.save_credentials(
                self.user_a, "123456", "broker-secret", "Demo-Server"
            )
            credentials = self.auth_store.get_credentials(self.user_a)

        self.assertEqual(credentials["password"], "broker-secret")
        with self.database.engine.connect() as connection:
            encrypted_value = connection.execute(
                text("SELECT password_encrypted FROM mt5_credentials")
            ).scalar_one()
        self.assertNotIn("broker-secret", encrypted_value)

    def test_sessions_are_opaque_and_revocable(self):
        token, _ = self.auth_store.create_session(self.user_a)

        self.assertEqual(
            self.auth_store.get_session_user(token)["user_id"], self.user_a
        )
        self.auth_store.delete_session(token)
        self.assertIsNone(self.auth_store.get_session_user(token))

    def test_password_reset_is_single_use_and_revokes_sessions(self):
        session_token, _ = self.auth_store.create_session(self.user_a)
        reset_token = "one-time-password-reset-token"
        self.assertTrue(
            self.auth_store.create_password_reset("a@example.test", reset_token)
        )

        self.assertTrue(
            self.auth_store.reset_password(
                reset_token, "new-correct-horse-battery-2"
            )
        )
        self.assertFalse(
            self.auth_store.reset_password(
                reset_token, "another-correct-horse-battery-3"
            )
        )
        self.assertIsNone(self.auth_store.get_session_user(session_token))
        self.assertIsNotNone(
            self.auth_store.authenticate(
                "a@example.test", "new-correct-horse-battery-2"
            )
        )

    def test_password_recovery_api_hides_account_existence(self):
        original_auth_store = main.auth_store
        main.auth_store = self.auth_store
        environment = {
            "MT5_ACCOUNT_RECOVERY_ENABLED": "1",
            "MT5_ACCOUNT_RECOVERY_READY": "1",
            "MT5_SMTP_HOST": "smtp.example.test",
            "MT5_SMTP_FROM": "noreply@example.test",
            "MT5_PUBLIC_BASE_URL": "https://optimizer.example.test",
            "MT5_LEGACY_BASIC_AUTH_ENABLED": "0",
        }
        try:
            with (
                patch.dict("os.environ", environment),
                patch.object(main, "send_account_email") as send_email,
                TestClient(main.app) as client,
            ):
                known = client.post(
                    "/auth/recovery", json={"email": "a@example.test"}
                )
                unknown = client.post(
                    "/auth/recovery", json={"email": "missing@example.test"}
                )

                self.assertEqual(known.status_code, 202)
                self.assertEqual(known.json(), unknown.json())
                send_email.assert_called_once()
                _, reset_token, purpose = send_email.call_args.args
                self.assertEqual(purpose, "password_reset")

                session_token, _ = self.auth_store.create_session(self.user_a)
                client.cookies.set(main.SESSION_COOKIE_NAME, session_token)
                reset = client.post(
                    "/auth/password-reset",
                    json={
                        "token": reset_token,
                        "new_password": "updated-correct-horse-battery",
                    },
                )
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(
                    client.get("/auth/me").status_code,
                    401,
                )
        finally:
            main.auth_store = original_auth_store

    def test_login_creates_session_and_credentials_can_be_saved(self):
        original_auth_store = main.auth_store
        main.auth_store = self.auth_store
        key = Fernet.generate_key().decode("ascii")
        try:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "MT5_CREDENTIAL_ENCRYPTION_KEY": key,
                        "MT5_COOKIE_SECURE": "0",
                    },
                ),
                TestClient(main.app) as client,
            ):
                response = client.post(
                    "/auth/login",
                    json={
                        "email": "a@example.test",
                        "password": "correct-horse-battery-1",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers.get("set-cookie"))

                saved = client.put(
                    "/account/mt5",
                    json={
                        "login": "123456",
                        "password": "broker-secret",
                        "server": "Demo-Server",
                    },
                )
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.json(), {"saved": True})
        finally:
            main.auth_store = original_auth_store

    def test_job_api_returns_not_found_for_another_users_job(self):
        self.job_store.create(
            "private-job",
            {"user_id": self.user_a},
            1,
            self.user_a,
        )
        set_filename = "trial_user_a_BEST_RESULT.set"
        report_filename = "report_trial_user_a.xlsx"
        self.job_store.update(
            "private-job",
            status="completed",
            result={
                "best_set_file": set_filename,
                "excel_reports": [{"filename": report_filename}],
            },
        )
        manager = OptimizationJobManager(
            self.job_store, self.root / "jobs", lambda *_: {}
        )
        original_manager = main.job_manager
        original_auth_store = main.auth_store
        original_workspace = main.WORKSPACE_DIR
        main.job_manager = manager
        main.auth_store = self.auth_store
        main.WORKSPACE_DIR = self.root / "workspace"
        user_workspace = main.WORKSPACE_DIR / "users" / self.user_a
        (user_workspace / "temp_configs").mkdir(parents=True)
        (user_workspace / "reports").mkdir(parents=True)
        (user_workspace / "temp_configs" / set_filename).write_text(
            "owned set", encoding="utf-8"
        )
        (user_workspace / "reports" / report_filename).write_bytes(b"owned report")
        try:
            with TestClient(main.app) as client:
                token_a, _ = self.auth_store.create_session(self.user_a)
                client.cookies.set(main.SESSION_COOKIE_NAME, token_a)
                own_response = client.get("/jobs/private-job")
                self.assertEqual(own_response.status_code, 200)
                self.assertEqual(
                    client.get(f"/results/{set_filename}").status_code, 200
                )
                self.assertEqual(
                    client.get(f"/reports/{report_filename}").status_code, 200
                )

                token_b, _ = self.auth_store.create_session(self.user_b)
                client.cookies.set(main.SESSION_COOKIE_NAME, token_b)
                other_response = client.get("/jobs/private-job")
                self.assertEqual(other_response.status_code, 404)
                self.assertEqual(
                    client.get(f"/results/{set_filename}").status_code, 404
                )
                self.assertEqual(
                    client.get(f"/reports/{report_filename}").status_code, 404
                )
        finally:
            main.job_manager = original_manager
            main.auth_store = original_auth_store
            main.WORKSPACE_DIR = original_workspace


if __name__ == "__main__":
    unittest.main()
