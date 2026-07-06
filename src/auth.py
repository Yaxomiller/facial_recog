from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import smtplib
from typing import Optional

import bcrypt

from src.db import init_database
from src.session_store import get_connection, get_session_store


USERNAME_ENV_KEYS = ("ADMIN_USERNAME", "ATTENDANCE_ADMIN_USERNAME")
PASSWORD_HASH_ENV_KEYS = ("ADMIN_PASSWORD_HASH", "ATTENDANCE_ADMIN_PASSWORD_HASH")
EMAIL_ENV_KEYS = ("ADMIN_EMAIL", "ATTENDANCE_ADMIN_EMAIL")
MIN_PASSWORD_LENGTH = 10
RECOVERY_CODE_LENGTH = 6
RECOVERY_CODE_TTL_SECONDS = int(os.getenv("ATTENDANCE_RECOVERY_CODE_TTL_SECONDS", "600"))
BACKUP_CODE_COUNT = int(os.getenv("ATTENDANCE_BACKUP_CODE_COUNT", "6"))
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool
    use_ssl: bool


@dataclass(frozen=True)
class AuthStatus:
    configured: bool
    setup_required: bool
    source: str
    email_configured: bool
    email_recovery_enabled: bool
    recovery_codes_available: bool = False


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truthy_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _get_setting(key: str) -> Optional[str]:
    _initialize_auth_store()
    with get_connection() as connection:
        row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def _set_setting(key: str, value: str) -> None:
    _initialize_auth_store()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def _delete_settings(keys: tuple[str, ...]) -> None:
    _initialize_auth_store()
    with get_connection() as connection:
        connection.executemany("DELETE FROM app_settings WHERE key = ?", [(key,) for key in keys])


_SMTP_SETTING_KEYS = (
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from_email",
    "smtp_use_tls",
    "smtp_use_ssl",
)


def _stored_email_settings() -> Optional[EmailSettings]:
    host = (_get_setting("smtp_host") or "").strip()
    from_email = (_get_setting("smtp_from_email") or "").strip()
    if not host or not from_email:
        return None

    use_ssl = (_get_setting("smtp_use_ssl") or "false").strip().lower() in {"1", "true", "yes", "on"}
    use_tls = (_get_setting("smtp_use_tls") or "true").strip().lower() not in {"0", "false", "no", "off"}
    port_text = (_get_setting("smtp_port") or "").strip() or ("465" if use_ssl else "587")
    try:
        port = int(port_text)
    except ValueError:
        port = 465 if use_ssl else 587

    return EmailSettings(
        host=host,
        port=port,
        username=(_get_setting("smtp_username") or "").strip(),
        password=_get_setting("smtp_password") or "",
        from_email=from_email,
        use_tls=use_tls,
        use_ssl=use_ssl,
    )


def save_email_settings(
    current_password: str,
    host: str,
    port: str,
    username: str,
    password: str,
    from_email: str,
    use_tls: bool = True,
    use_ssl: bool = False,
) -> None:
    # Changing where recovery codes are delivered is account-takeover
    # sensitive, so re-verify the password even inside a session.
    admin_username = get_admin_username()
    if not admin_username or not authenticate_admin(admin_username, current_password):
        raise RuntimeError("Current password is incorrect.")

    normalized_host = host.strip()
    normalized_from = from_email.strip()
    if not normalized_host or not normalized_from:
        _delete_settings(_SMTP_SETTING_KEYS)
        return
    _normalize_email(normalized_from)
    if port.strip():
        try:
            int(port.strip())
        except ValueError as exc:
            raise ValueError("SMTP port must be a number.") from exc

    _set_setting("smtp_host", normalized_host)
    _set_setting("smtp_port", port.strip())
    _set_setting("smtp_username", username.strip())
    if password:
        _set_setting("smtp_password", password)
    _set_setting("smtp_from_email", normalized_from)
    _set_setting("smtp_use_tls", "true" if use_tls else "false")
    _set_setting("smtp_use_ssl", "true" if use_ssl else "false")


def get_email_settings_public() -> dict:
    stored = _stored_email_settings()
    env_settings = _env_email_settings()
    active = stored or env_settings
    return {
        "configured": active is not None,
        "source": "app" if stored else ("environment" if env_settings else "none"),
        "host": active.host if active else "",
        "port": str(active.port) if active else "",
        "username": active.username if active else "",
        "from_email": active.from_email if active else "",
        "use_tls": active.use_tls if active else True,
        "use_ssl": active.use_ssl if active else False,
        "has_password": bool(active.password) if active else False,
    }


def _env_email_settings() -> Optional[EmailSettings]:
    host = os.getenv("ATTENDANCE_SMTP_HOST", "").strip()
    port_text = os.getenv("ATTENDANCE_SMTP_PORT", "").strip() or ("465" if _truthy_env("ATTENDANCE_SMTP_USE_SSL", False) else "587")
    from_email = os.getenv("ATTENDANCE_SMTP_FROM_EMAIL", "").strip()
    if not host or not from_email:
        return None

    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError("ATTENDANCE_SMTP_PORT must be a valid number.") from exc

    return EmailSettings(
        host=host,
        port=port,
        username=os.getenv("ATTENDANCE_SMTP_USERNAME", "").strip(),
        password=os.getenv("ATTENDANCE_SMTP_PASSWORD", ""),
        from_email=from_email,
        use_tls=_truthy_env("ATTENDANCE_SMTP_USE_TLS", True),
        use_ssl=_truthy_env("ATTENDANCE_SMTP_USE_SSL", False),
    )


def _email_settings() -> Optional[EmailSettings]:
    # Settings saved in the app take precedence; env vars remain a fallback
    # for headless/scripted deployments.
    return _stored_email_settings() or _env_email_settings()


def _initialize_auth_store() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash BLOB NOT NULL,
                email TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(admin_credentials)").fetchall()
        }
        if "email" not in columns:
            connection.execute("ALTER TABLE admin_credentials ADD COLUMN email TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT NOT NULL,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_recovery_codes_lookup
            ON admin_recovery_codes(purpose, email, expires_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_backup_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def _fetch_local_credentials() -> Optional[sqlite3.Row]:
    _initialize_auth_store()
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT id, username, password_hash, email, created_at, updated_at
            FROM admin_credentials
            WHERE id = 1
            """
        ).fetchone()


def _env_admin_username() -> Optional[str]:
    return _first_env(USERNAME_ENV_KEYS)


def _env_admin_password_hash() -> Optional[bytes]:
    value = _first_env(PASSWORD_HASH_ENV_KEYS)
    return value.encode("utf-8") if value else None


def _env_admin_email() -> Optional[str]:
    value = _first_env(EMAIL_ENV_KEYS)
    return _normalize_email(value) if value else None


def _is_valid_bcrypt_hash(password_hash: Optional[bytes]) -> bool:
    return bool(password_hash and password_hash.startswith((b"$2a$", b"$2b$", b"$2y$")))


def _normalize_username(username: str) -> str:
    normalized = username.strip()
    if not normalized:
        raise ValueError("Username cannot be empty.")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Username must be 3 to 64 characters and use only letters, numbers, dots, underscores, hyphens, or @."
        )
    return normalized


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("Email cannot be empty.")
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def _validate_password(password: str) -> None:
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"be at least {MIN_PASSWORD_LENGTH} characters long")
    if not any(character.islower() for character in password):
        errors.append("include a lowercase letter")
    if not any(character.isupper() for character in password):
        errors.append("include an uppercase letter")
    if not any(character.isdigit() for character in password):
        errors.append("include a number")
    if not any(not character.isalnum() for character in password):
        errors.append("include a symbol")
    if errors:
        raise ValueError("Password must " + ", ".join(errors) + ".")


def _write_local_credentials(username: str, password: str, email: Optional[str] = None) -> None:
    normalized_username = _normalize_username(username)
    _validate_password(password)
    local_credentials = _fetch_local_credentials()
    existing_email = (
        str(local_credentials["email"]).strip()
        if local_credentials is not None and local_credentials["email"]
        else _env_admin_email()
    )
    normalized_email = _normalize_email(email) if email is not None else existing_email
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    now = _utc_now_iso()
    _initialize_auth_store()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO admin_credentials (id, username, password_hash, email, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                password_hash = excluded.password_hash,
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (normalized_username, password_hash, normalized_email, now, now),
        )


def get_admin_username() -> Optional[str]:
    local_credentials = _fetch_local_credentials()
    if local_credentials is not None:
        return str(local_credentials["username"])
    return _env_admin_username()


def get_admin_password_hash() -> Optional[bytes]:
    local_credentials = _fetch_local_credentials()
    if local_credentials is not None:
        return bytes(local_credentials["password_hash"])
    return _env_admin_password_hash()


def get_admin_email() -> Optional[str]:
    local_credentials = _fetch_local_credentials()
    if local_credentials is not None and local_credentials["email"]:
        return _normalize_email(str(local_credentials["email"]))
    return _env_admin_email()


def email_recovery_enabled() -> bool:
    return bool(get_admin_email() and _email_settings() is not None)


def get_auth_status() -> AuthStatus:
    local_credentials = _fetch_local_credentials()
    if local_credentials is not None:
        return AuthStatus(
            configured=True,
            setup_required=False,
            source="local",
            email_configured=bool(local_credentials["email"]),
            email_recovery_enabled=email_recovery_enabled(),
            recovery_codes_available=recovery_backup_codes_available(),
        )

    env_username = _env_admin_username()
    env_password_hash = _env_admin_password_hash()
    if env_username and env_password_hash:
        return AuthStatus(
            configured=True,
            setup_required=False,
            source="environment",
            email_configured=bool(_env_admin_email()),
            email_recovery_enabled=email_recovery_enabled(),
        )

    return AuthStatus(
        configured=False,
        setup_required=True,
        source="none",
        email_configured=False,
        email_recovery_enabled=False,
    )


def is_admin_auth_configured() -> bool:
    return get_auth_status().configured


def ensure_admin_auth_config(allow_bootstrap: bool = False) -> None:
    status = get_auth_status()
    if not status.configured:
        if allow_bootstrap:
            return
        raise RuntimeError(
            "Admin authentication is not configured. Create credentials from the login screen or set ADMIN_USERNAME and ADMIN_PASSWORD_HASH."
        )

    password_hash = get_admin_password_hash()
    if not _is_valid_bcrypt_hash(password_hash):
        raise RuntimeError("ADMIN_PASSWORD_HASH is not a valid bcrypt hash.")


def setup_admin_credentials(username: str, password: str, email: Optional[str] = None) -> AuthStatus:
    normalized_email = _normalize_email(email) if email is not None else None
    status = get_auth_status()
    existing_email = get_admin_email()
    if existing_email and normalized_email and hmac.compare_digest(existing_email, normalized_email):
        raise RuntimeError("Registered email already exists. Please log in.")
    if status.configured:
        raise RuntimeError("An account is already configured. Please log in.")
    _write_local_credentials(username=username, password=password, email=normalized_email)
    get_session_store().clear_all_sessions()
    return get_auth_status()


def reset_admin_credentials(
    username: str,
    current_password: str,
    new_password: str,
) -> AuthStatus:
    # Changing the password over the network requires proving knowledge of
    # the current password. Without this check anyone on the LAN who knew
    # the username could take over the device.
    if not authenticate_admin(username, current_password):
        raise RuntimeError("Current username or password is incorrect.")
    _write_local_credentials(username=username.strip(), password=new_password)
    get_session_store().clear_all_sessions()
    return get_auth_status()


def reset_admin_credentials_console(
    username: str,
    new_password: str,
    email: Optional[str] = None,
) -> AuthStatus:
    # Offline recovery for someone with device console access (SSH/keyboard).
    # This is the appliance equivalent of a root password reset: physical or
    # shell access to the device is the authorization. Never expose this over
    # the network.
    normalized_email = _normalize_email(email) if email else None
    _write_local_credentials(username=username, password=new_password, email=normalized_email)
    get_session_store().clear_all_sessions()
    return get_auth_status()


def _purge_recovery_codes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        DELETE FROM admin_recovery_codes
        WHERE expires_at <= ? OR consumed_at IS NOT NULL
        """,
        (_utc_now_iso(),),
    )


def _delete_recovery_codes(connection: sqlite3.Connection, purpose: str, email: str) -> None:
    connection.execute(
        "DELETE FROM admin_recovery_codes WHERE purpose = ? AND email = ?",
        (purpose, email),
    )


def _hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_recovery_code() -> str:
    return f"{secrets.randbelow(10 ** RECOVERY_CODE_LENGTH):0{RECOVERY_CODE_LENGTH}d}"


def _store_recovery_code(purpose: str, email: str, code: str) -> None:
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(seconds=RECOVERY_CODE_TTL_SECONDS)
    with get_connection() as connection:
        _purge_recovery_codes(connection)
        _delete_recovery_codes(connection, purpose, email)
        connection.execute(
            """
            INSERT INTO admin_recovery_codes (purpose, email, code_hash, created_at, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                purpose,
                email,
                _hash_recovery_code(code),
                created_at.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
            ),
        )


def _consume_recovery_code(purpose: str, email: str, code: str) -> None:
    normalized_email = _normalize_email(email)
    normalized_code = code.strip()
    if not normalized_code:
        raise RuntimeError("Verification code is required.")

    with get_connection() as connection:
        _purge_recovery_codes(connection)
        row = connection.execute(
            """
            SELECT id, code_hash
            FROM admin_recovery_codes
            WHERE purpose = ? AND email = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (purpose, normalized_email),
        ).fetchone()
        if row is None or not hmac.compare_digest(str(row["code_hash"]), _hash_recovery_code(normalized_code)):
            raise RuntimeError("Invalid or expired verification code.")
        connection.execute(
            """
            UPDATE admin_recovery_codes
            SET consumed_at = ?
            WHERE id = ?
            """,
            (_utc_now_iso(), row["id"]),
        )


def _send_email(to_email: str, subject: str, body: str) -> None:
    settings = _email_settings()
    if settings is None:
        raise RuntimeError("Email recovery is not configured. Set ATTENDANCE_SMTP_HOST and ATTENDANCE_SMTP_FROM_EMAIL.")

    message = EmailMessage()
    message["From"] = settings.from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        if settings.use_ssl:
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=15) as smtp:
                if settings.username:
                    smtp.login(settings.username, settings.password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(settings.host, settings.port, timeout=15) as smtp:
            if settings.use_tls:
                smtp.starttls()
            if settings.username:
                smtp.login(settings.username, settings.password)
            smtp.send_message(message)
    except OSError as exc:
        raise RuntimeError("Verification email could not be sent.") from exc


def _request_recovery_code(email: str, purpose: str) -> str:
    if not get_admin_email():
        raise RuntimeError("No recovery email is registered for this login.")
    if _email_settings() is None:
        raise RuntimeError("Email recovery is not configured. Set ATTENDANCE_SMTP_HOST and ATTENDANCE_SMTP_FROM_EMAIL.")

    normalized_email = _normalize_email(email)
    expected_email = get_admin_email()
    message = "If the email matches the registered email, a verification code has been sent."
    if not expected_email or not hmac.compare_digest(normalized_email, expected_email):
        return message

    code = _generate_recovery_code()
    _store_recovery_code(purpose, normalized_email, code)
    subject = "Attendance login verification code"
    action = "username reminder" if purpose == "username" else "password reset"
    body = (
        f"Your verification code for {action} is {code}.\n"
        f"It expires in {RECOVERY_CODE_TTL_SECONDS // 60} minutes."
    )
    try:
        _send_email(normalized_email, subject, body)
    except RuntimeError:
        with get_connection() as connection:
            _delete_recovery_codes(connection, purpose, normalized_email)
        raise
    return message


def request_username_recovery(email: str) -> str:
    return _request_recovery_code(email=email, purpose="username")


def request_password_recovery(email: str) -> str:
    return _request_recovery_code(email=email, purpose="password")


def recover_username_by_email(email: str, code: str) -> str:
    _consume_recovery_code("username", email, code)
    username = get_admin_username()
    if not username:
        raise RuntimeError("Username is not configured.")
    return username


def reset_admin_password_with_email(email: str, code: str, new_password: str) -> AuthStatus:
    normalized_email = _normalize_email(email)
    _consume_recovery_code("password", normalized_email, code)
    username = get_admin_username()
    if not username:
        raise RuntimeError("Username is not configured.")
    _write_local_credentials(username=username, password=new_password, email=normalized_email)
    get_session_store().clear_all_sessions()
    return get_auth_status()


def _normalize_backup_code(code: str) -> str:
    return re.sub(r"[^A-F0-9]", "", code.strip().upper())


def _format_backup_code(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:]}"


def generate_recovery_backup_codes(current_password: str) -> list[str]:
    # Offline in-app recovery: one-time codes the admin saves somewhere safe.
    # Generating a fresh batch is a sensitive action, so it requires proving
    # the current password even inside an authenticated session, and it
    # invalidates every previously issued code.
    admin_username = get_admin_username()
    if not admin_username or not authenticate_admin(admin_username, current_password):
        raise RuntimeError("Current password is incorrect.")

    raw_codes = [secrets.token_hex(4).upper() for _ in range(BACKUP_CODE_COUNT)]
    now = _utc_now_iso()
    _initialize_auth_store()
    with get_connection() as connection:
        connection.execute("DELETE FROM admin_backup_codes")
        connection.executemany(
            "INSERT INTO admin_backup_codes (code_hash, created_at, consumed_at) VALUES (?, ?, NULL)",
            [(_hash_recovery_code(raw), now) for raw in raw_codes],
        )
    return [_format_backup_code(raw) for raw in raw_codes]


def recovery_backup_codes_remaining() -> int:
    _initialize_auth_store()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS remaining FROM admin_backup_codes WHERE consumed_at IS NULL"
        ).fetchone()
    return int(row["remaining"]) if row else 0


def reset_admin_password_with_backup_code(code: str, new_password: str) -> str:
    normalized = _normalize_backup_code(code)
    if not normalized:
        raise RuntimeError("Recovery code is required.")

    code_hash = _hash_recovery_code(normalized)
    _initialize_auth_store()
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, code_hash FROM admin_backup_codes WHERE consumed_at IS NULL"
        ).fetchall()
        matched_id = None
        for row in rows:
            if hmac.compare_digest(str(row["code_hash"]), code_hash):
                matched_id = int(row["id"])
                break
        if matched_id is None:
            raise RuntimeError("Invalid or already-used recovery code.")
        connection.execute(
            "UPDATE admin_backup_codes SET consumed_at = ? WHERE id = ?",
            (_utc_now_iso(), matched_id),
        )

    username = get_admin_username()
    if not username:
        raise RuntimeError("No admin account is configured on this device.")
    _write_local_credentials(username=username, password=new_password)
    get_session_store().clear_all_sessions()
    return username


def recovery_backup_codes_available() -> bool:
    return recovery_backup_codes_remaining() > 0


def set_admin_recovery_email(current_password: str, email: str) -> None:
    admin_username = get_admin_username()
    if not admin_username or not authenticate_admin(admin_username, current_password):
        raise RuntimeError("Current password is incorrect.")
    normalized_email = _normalize_email(email)
    _initialize_auth_store()
    with get_connection() as connection:
        connection.execute(
            "UPDATE admin_credentials SET email = ?, updated_at = ? WHERE id = 1",
            (normalized_email, _utc_now_iso()),
        )


def send_recovery_test_email() -> str:
    recipient = get_admin_email()
    if not recipient:
        raise RuntimeError("Register a recovery email first.")
    _send_email(
        recipient,
        "Attendance email recovery test",
        "Email recovery is configured correctly on your attendance device.",
    )
    return f"Test email sent to {recipient}."


def request_reregister_code(email: str) -> str:
    # Sends a verification code to the REGISTERED email so its owner can
    # re-register the device admin account (new username/password) from the
    # sign-up screen.
    return _request_recovery_code(email=email, purpose="reregister")


def reregister_admin(username: str, password: str, email: str, code: str) -> AuthStatus:
    # Re-registering replaces the device admin account, so it demands proof
    # of ownership: a saved recovery code OR a code emailed to the currently
    # registered address. Without this gate, anyone at the kiosk could take
    # over the device through the sign-up form.
    normalized_code = code.strip()
    if not normalized_code:
        raise RuntimeError("Enter a recovery code or the emailed verification code.")

    verified = False
    backup_normalized = _normalize_backup_code(normalized_code)
    if backup_normalized:
        code_hash = _hash_recovery_code(backup_normalized)
        _initialize_auth_store()
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT id, code_hash FROM admin_backup_codes WHERE consumed_at IS NULL"
            ).fetchall()
            for row in rows:
                if hmac.compare_digest(str(row["code_hash"]), code_hash):
                    connection.execute(
                        "UPDATE admin_backup_codes SET consumed_at = ? WHERE id = ?",
                        (_utc_now_iso(), int(row["id"])),
                    )
                    verified = True
                    break

    if not verified:
        registered_email = get_admin_email()
        if not registered_email:
            raise RuntimeError(
                "Invalid verification. Use a saved recovery code, or ask the administrator to reset from the device console."
            )
        _consume_recovery_code("reregister", registered_email, normalized_code)
        verified = True

    _write_local_credentials(username=username, password=password, email=_normalize_email(email))
    get_session_store().clear_all_sessions()
    return get_auth_status()


def authenticate_admin(username: str, password: str) -> bool:
    init_database()
    expected_username = get_admin_username()
    stored_hash = get_admin_password_hash()
    if not expected_username or not stored_hash:
        return False

    username_ok = hmac.compare_digest(username.strip(), expected_username)
    try:
        password_ok = bcrypt.checkpw(password.encode("utf-8"), stored_hash)
    except ValueError:
        return False
    return username_ok and password_ok
