from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import src.auth as auth
import src.db as attendance_db
import src.session_store as session_store


class ResetCredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        self.original_attendance_db = attendance_db.DATABASE_FILE
        self.original_session_db = session_store.SESSION_DB_FILE
        attendance_db.DATABASE_FILE = temp_path / "attendance.db"
        session_store.SESSION_DB_FILE = temp_path / "session_store.db"
        session_store.get_session_store.cache_clear()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "ADMIN_USERNAME": "",
                "ATTENDANCE_ADMIN_USERNAME": "",
                "ADMIN_PASSWORD_HASH": "",
                "ATTENDANCE_ADMIN_PASSWORD_HASH": "",
                "ADMIN_EMAIL": "",
                "ATTENDANCE_ADMIN_EMAIL": "",
                "ATTENDANCE_SMTP_HOST": "",
                "ATTENDANCE_SMTP_PORT": "",
                "ATTENDANCE_SMTP_FROM_EMAIL": "",
            },
            clear=False,
        )
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        session_store.get_session_store.cache_clear()
        attendance_db.DATABASE_FILE = self.original_attendance_db
        session_store.SESSION_DB_FILE = self.original_session_db
        self.temp_dir.cleanup()

    def test_reset_credentials_updates_password_for_existing_username(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")

        auth.reset_admin_credentials(
            username="admin.user",
            current_password="OldPassword1!",
            new_password="NewPassword1!",
        )

        self.assertFalse(auth.authenticate_admin("admin.user", "OldPassword1!"))
        self.assertTrue(auth.authenticate_admin("admin.user", "NewPassword1!"))
        self.assertEqual(auth.get_admin_username(), "admin.user")

    def test_reset_credentials_requires_the_current_password(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")
        store = session_store.get_session_store()
        original_session = store.create_session("admin.user")

        with self.assertRaisesRegex(RuntimeError, "Current username or password is incorrect."):
            auth.reset_admin_credentials(
                username="admin.user",
                current_password="WrongPassword1!",
                new_password="NewPassword1!",
            )

        with self.assertRaisesRegex(RuntimeError, "Current username or password is incorrect."):
            auth.reset_admin_credentials(
                username="wrong.user",
                current_password="OldPassword1!",
                new_password="NewPassword1!",
            )

        self.assertTrue(auth.authenticate_admin("admin.user", "OldPassword1!"))
        self.assertIsNotNone(store.get_session(original_session.session_id))

        auth.reset_admin_credentials(
            username="admin.user",
            current_password="OldPassword1!",
            new_password="NewPassword1!",
        )

        self.assertIsNone(store.get_session(original_session.session_id))

    def test_console_reset_replaces_credentials_without_current_password(self) -> None:
        auth.setup_admin_credentials(username="forgotten.user", password="OldPassword1!")
        store = session_store.get_session_store()
        original_session = store.create_session("forgotten.user")

        auth.reset_admin_credentials_console(
            username="recovered.user",
            new_password="NewPassword1!",
            email="recovery@example.com",
        )

        self.assertTrue(auth.authenticate_admin("recovered.user", "NewPassword1!"))
        self.assertFalse(auth.authenticate_admin("forgotten.user", "OldPassword1!"))
        self.assertEqual(auth.get_admin_email(), "recovery@example.com")
        self.assertIsNone(store.get_session(original_session.session_id))

    def test_console_reset_still_enforces_password_strength(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")

        with self.assertRaisesRegex(ValueError, "Password must"):
            auth.reset_admin_credentials_console(
                username="admin.user",
                new_password="weak",
            )

        self.assertTrue(auth.authenticate_admin("admin.user", "OldPassword1!"))

    def test_generating_recovery_codes_requires_the_current_password(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")

        with self.assertRaisesRegex(RuntimeError, "Current password is incorrect."):
            auth.generate_recovery_backup_codes("WrongPassword1!")

        codes = auth.generate_recovery_backup_codes("OldPassword1!")
        self.assertEqual(len(codes), auth.BACKUP_CODE_COUNT)
        self.assertEqual(auth.recovery_backup_codes_remaining(), auth.BACKUP_CODE_COUNT)
        for code in codes:
            self.assertRegex(code, r"^[0-9A-F]{4}-[0-9A-F]{4}$")

    def test_recovery_code_resets_password_once_and_only_once(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")
        store = session_store.get_session_store()
        original_session = store.create_session("admin.user")
        codes = auth.generate_recovery_backup_codes("OldPassword1!")

        # Codes are entered by humans: tolerate lowercase and missing hyphen.
        username = auth.reset_admin_password_with_backup_code(
            codes[0].lower().replace("-", ""),
            "NewPassword1!",
        )

        self.assertEqual(username, "admin.user")
        self.assertTrue(auth.authenticate_admin("admin.user", "NewPassword1!"))
        self.assertFalse(auth.authenticate_admin("admin.user", "OldPassword1!"))
        self.assertIsNone(store.get_session(original_session.session_id))
        self.assertEqual(auth.recovery_backup_codes_remaining(), auth.BACKUP_CODE_COUNT - 1)

        with self.assertRaisesRegex(RuntimeError, "Invalid or already-used recovery code."):
            auth.reset_admin_password_with_backup_code(codes[0], "AnotherPassword1!")

    def test_invalid_recovery_code_is_rejected(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")
        auth.generate_recovery_backup_codes("OldPassword1!")

        with self.assertRaisesRegex(RuntimeError, "Invalid or already-used recovery code."):
            auth.reset_admin_password_with_backup_code("0000-0000", "NewPassword1!")

        self.assertTrue(auth.authenticate_admin("admin.user", "OldPassword1!"))

    def test_regenerating_codes_invalidates_previous_batch(self) -> None:
        auth.setup_admin_credentials(username="admin.user", password="OldPassword1!")
        first_batch = auth.generate_recovery_backup_codes("OldPassword1!")
        auth.generate_recovery_backup_codes("OldPassword1!")

        with self.assertRaisesRegex(RuntimeError, "Invalid or already-used recovery code."):
            auth.reset_admin_password_with_backup_code(first_batch[0], "NewPassword1!")

    def test_setup_credentials_store_registered_email(self) -> None:
        auth.setup_admin_credentials(
            username="admin.user",
            password="OldPassword1!",
            email="admin@example.com",
        )

        self.assertEqual(auth.get_admin_email(), "admin@example.com")

    def test_setup_rejects_duplicate_registered_email(self) -> None:
        auth.setup_admin_credentials(
            username="admin.user",
            password="OldPassword1!",
            email="admin@example.com",
        )

        with self.assertRaisesRegex(RuntimeError, "Registered email already exists. Please log in."):
            auth.setup_admin_credentials(
                username="another.user",
                password="AnotherPassword1!",
                email="admin@example.com",
            )

    def test_username_recovery_returns_username_after_verification(self) -> None:
        auth.setup_admin_credentials(
            username="admin.user",
            password="OldPassword1!",
            email="admin@example.com",
        )

        with patch.dict(
            os.environ,
            {
                "ATTENDANCE_SMTP_HOST": "smtp.example.com",
                "ATTENDANCE_SMTP_PORT": "587",
                "ATTENDANCE_SMTP_FROM_EMAIL": "noreply@example.com",
            },
            clear=False,
        ):
            with patch("src.auth._generate_recovery_code", return_value="123456"), patch("src.auth._send_email") as send_email:
                message = auth.request_username_recovery("admin@example.com")

        self.assertIn("verification code", message.lower())
        send_email.assert_called_once()
        self.assertEqual(
            auth.recover_username_by_email("admin@example.com", "123456"),
            "admin.user",
        )

    def test_password_recovery_resets_password_and_clears_sessions(self) -> None:
        auth.setup_admin_credentials(
            username="admin.user",
            password="OldPassword1!",
            email="admin@example.com",
        )
        store = session_store.get_session_store()
        original_session = store.create_session("admin.user")

        with patch.dict(
            os.environ,
            {
                "ATTENDANCE_SMTP_HOST": "smtp.example.com",
                "ATTENDANCE_SMTP_PORT": "587",
                "ATTENDANCE_SMTP_FROM_EMAIL": "noreply@example.com",
            },
            clear=False,
        ):
            with patch("src.auth._generate_recovery_code", return_value="654321"), patch("src.auth._send_email"):
                auth.request_password_recovery("admin@example.com")

        auth.reset_admin_password_with_email(
            email="admin@example.com",
            code="654321",
            new_password="NewPassword1!",
        )

        self.assertTrue(auth.authenticate_admin("admin.user", "NewPassword1!"))
        self.assertFalse(auth.authenticate_admin("admin.user", "OldPassword1!"))
        self.assertIsNone(store.get_session(original_session.session_id))


if __name__ == "__main__":
    unittest.main()
