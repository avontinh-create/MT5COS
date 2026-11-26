import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from app.services.deployment_check import check_deployment


class DeploymentCheckTests(unittest.TestCase):
    def test_incomplete_configuration_fails_without_exposing_secrets(self):
        secret = "not-for-output"
        results = check_deployment(
            {
                "DATABASE_URL": f"postgresql://user:{secret}@localhost/app",
                "MT5_SMTP_PASSWORD": secret,
            },
            database_probe=lambda _: ("FAIL", "database unavailable"),
        )

        self.assertTrue(any(result.status == "FAIL" for result in results))
        self.assertTrue(
            all(secret not in result.detail for result in results)
        )
        registration = next(
            result for result in results if result.name == "Public registration"
        )
        self.assertEqual(registration.status, "INFO")

    def test_ready_configuration_passes_while_signup_stays_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "mt5_portable" / "terminal64.exe"
            expert = terminal.parent / "MQL5" / "Experts" / "Peak.ex5"
            terminal.parent.mkdir(parents=True)
            expert.parent.mkdir(parents=True)
            terminal.touch()
            expert.touch()

            environment = {
                "DATABASE_URL": "postgresql+psycopg://user:pass@db/app",
                "MT5_PUBLIC_BASE_URL": "https://optimizer.example.com",
                "MT5_SMTP_HOST": "smtp.gmail.com",
                "MT5_SMTP_PORT": "465",
                "MT5_SMTP_USE_SSL": "1",
                "MT5_SMTP_USERNAME": "optimizer@example.com",
                "MT5_SMTP_PASSWORD": "smtp-secret",
                "MT5_SMTP_FROM": "optimizer@example.com",
                "MT5_CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "MT5_TERMINAL_PATH": str(terminal),
                "MT5_EXPERT_NAME": "Peak.ex5",
                "MT5_API_HOST": "127.0.0.1",
                "MT5_EMAIL_VERIFICATION_ENABLED": "1",
                "MT5_ACCOUNT_RECOVERY_ENABLED": "1",
                "MT5_ACCOUNT_RECOVERY_READY": "1",
                "MT5_COOKIE_SECURE": "1",
                "MT5_LEGACY_BASIC_AUTH_ENABLED": "0",
                "MT5_DRY_RUN": "0",
            }
            results = check_deployment(
                environment,
                project_root=root,
                database_probe=lambda _: (
                    "PASS",
                    "PostgreSQL connection and schema revision are ready.",
                ),
            )

        failures = [result for result in results if result.status == "FAIL"]
        self.assertEqual(failures, [])
        registration = next(
            result for result in results if result.name == "Public registration"
        )
        self.assertEqual(registration.status, "INFO")

    def test_invalid_smtp_auth_pair_and_public_url_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            results = check_deployment(
                {
                    "DATABASE_URL": "postgresql://db",
                    "MT5_PUBLIC_BASE_URL": "http://optimizer.example.com",
                    "MT5_SMTP_HOST": "smtp.gmail.com",
                    "MT5_SMTP_FROM": "optimizer@example.com",
                    "MT5_SMTP_USERNAME": "optimizer@example.com",
                    "MT5_SMTP_PASSWORD": "",
                },
                project_root=Path(directory),
                database_probe=lambda _: ("PASS", "database ready"),
            )

        self.assertEqual(
            next(result for result in results if result.name == "HTTPS URL").status,
            "FAIL",
        )
        self.assertEqual(
            next(result for result in results if result.name == "SMTP").status,
            "FAIL",
        )


if __name__ == "__main__":
    unittest.main()
