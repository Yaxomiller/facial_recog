from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
import os
from pathlib import Path
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.auth import (
    authenticate_admin,
    email_recovery_enabled,
    ensure_admin_auth_config,
    generate_recovery_backup_codes,
    get_admin_email,
    get_admin_username,
    get_auth_status,
    get_email_settings_public,
    is_admin_auth_configured,
    recover_username_by_email,
    recovery_backup_codes_remaining,
    request_password_recovery,
    request_reregister_code,
    request_username_recovery,
    reregister_admin,
    reset_admin_credentials,
    reset_admin_password_with_backup_code,
    reset_admin_password_with_email,
    save_email_settings,
    send_recovery_test_email,
    set_admin_recovery_email,
    setup_admin_credentials,
)
from src.local_camera_proxy import LocalCameraProxy
from src.power_monitor import PowerMonitor
from src.session_store import SessionState, get_session_store
from src.v2.config import (
    MAX_PROFILE_CENTROID_THRESHOLD,
    OPEN_SET_MIN_CENTROID_SCORE,
    SINGLE_PROFILE_MIN_CENTROID_SCORE,
)
from src.v2.schemas import (
    ArchitectureNote,
    AttendanceRow,
    BreathCheckResult,
    BreathCheckSessionStartResult,
    BreathTestSessionCancelResult,
    BreathTestSessionStartResult,
    BreathTestResult,
    DeleteAttendanceResult,
    DeleteWorkerResult,
    DetectionResult,
    EnrollmentResult,
    IndexStats,
    RecognitionResult,
    ServiceStatus,
    WorkerRead,
)
from src.v2.service import ScalableAttendanceService


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIST_DIR = BASE_DIR / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"


def _cors_allow_origins() -> list[str]:
    configured_origins = os.getenv("ATTENDANCE_CORS_ALLOW_ORIGINS", "").strip()
    if configured_origins == "*":
        return ["*"]
    if configured_origins:
        return [origin.strip().rstrip("/") for origin in configured_origins.split(",") if origin.strip()]
    return [
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://0.0.0.0:4173",
        "http://0.0.0.0:5173",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ]


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_at: datetime


class AuthStatusResponse(BaseModel):
    configured: bool
    setup_required: bool
    source: str
    email_configured: bool
    email_recovery_enabled: bool
    recovery_codes_available: bool = False


class GenerateRecoveryCodesRequest(BaseModel):
    current_password: str


class RecoveryCodesResponse(BaseModel):
    codes: list[str]
    remaining: int


class RecoveryCodeResetRequest(BaseModel):
    code: str
    new_password: str
    confirm_password: str


class EmailSettingsRequest(BaseModel):
    current_password: str
    host: str
    port: str = ""
    username: str = ""
    password: str = ""
    from_email: str
    use_tls: bool = True
    use_ssl: bool = False


class RecoveryEmailRequest(BaseModel):
    current_password: str
    email: str


class ReregisterRequest(BaseModel):
    username: str
    email: str
    password: str
    confirm_password: str
    code: str


class ResetCredentialsResponse(BaseModel):
    ok: bool
    username: str
    message: str


class RecoveryRequest(BaseModel):
    email: str


class RecoveryVerifyRequest(BaseModel):
    email: str
    code: str


class PasswordRecoveryVerifyRequest(BaseModel):
    email: str
    code: str
    new_password: str
    confirm_password: str


class RecoveryRequestResponse(BaseModel):
    ok: bool
    message: str


class UsernameRecoveryResponse(BaseModel):
    ok: bool
    username: str
    message: str


class MeResponse(BaseModel):
    username: str
    expires_at: datetime


class SetupCredentialsRequest(BaseModel):
    username: str
    email: str
    password: str
    confirm_password: str


class ResetCredentialsRequest(BaseModel):
    username: str
    current_password: str
    new_password: str
    confirm_password: str


class BreathTestRequest(BaseModel):
    worker_id: int
    camera_id: str
    matched_score: float


class BreathTestStartRequest(BaseModel):
    worker_id: int
    camera_id: str


class BreathTestCompleteRequest(BaseModel):
    session_id: str
    matched_score: float


class BreathCheckStartRequest(BaseModel):
    # No worker: a breath check measures the sensor alone.
    camera_id: str = "breath-check"


class BreathCheckCompleteRequest(BaseModel):
    session_id: str


class LocalCameraSessionResponse(BaseModel):
    ok: bool
    running: bool
    mode: str
    source_name: str
    frame_path: str


class LocalCameraStopResponse(BaseModel):
    ok: bool
    running: bool


app = FastAPI(
    title="Industrial Facial Attendance API",
    version="3.0.0",
    description="FastAPI backend for the operator-facing React attendance application.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
service = ScalableAttendanceService()
session_store = get_session_store()
local_camera_proxy = LocalCameraProxy()
# TEMPORARY TESTING: samples board power draw while the app runs. Reports
# itself disabled (with a reason) when the monitor chip is absent.
power_monitor = PowerMonitor()


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    # Without this, Starlette returns a bare "Internal Server Error" as
    # text/plain and the real reason only appears in the server log. Return
    # JSON with the actual message so the operator UI can show what went wrong.
    import traceback

    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="assets")


def _extract_token(authorization: Optional[str], x_auth_token: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_auth_token:
        return x_auth_token.strip()
    return None


def _frontend_response(path: Path) -> FileResponse:
    response = FileResponse(path)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def demo_mode_enabled() -> bool:
    # DEMO MODE: when ATTENDANCE_DEMO_MODE is on, every authenticated endpoint
    # is reachable WITHOUT a login. This exists only for the offline
    # demonstration build (`python app.py demo`) on low-memory devices. It is
    # OFF by default; `kiosk`, `web`, `simple`, and `native` never set it, so
    # their authentication is unaffected. Never enable it on a networked or
    # production device.
    return os.getenv("ATTENDANCE_DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _demo_session() -> SessionState:
    return SessionState(
        session_id="demo-mode",
        username="demo",
        expires_at=datetime.utcnow() + timedelta(days=365),
    )


def require_auth(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> SessionState:
    if demo_mode_enabled():
        return _demo_session()
    token = _extract_token(authorization, x_auth_token)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session = session_store.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


def require_camera_auth(
    token: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> SessionState:
    if demo_mode_enabled():
        return _demo_session()
    resolved_token = _extract_token(authorization, x_auth_token)
    if not resolved_token and token:
        resolved_token = token.strip()
    if not resolved_token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session = session_store.get_session(resolved_token)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


@app.on_event("startup")
def verify_session_backend() -> None:
    ensure_admin_auth_config(allow_bootstrap=True)
    session_store.ping()
    print(
        "Recognition config active: "
        f"open-set centroid floor={OPEN_SET_MIN_CENTROID_SCORE:.3f}, "
        f"single-profile centroid floor={SINGLE_PROFILE_MIN_CENTROID_SCORE:.3f}, "
        f"centroid cap={MAX_PROFILE_CENTROID_THRESHOLD:.3f}"
    )
    power_monitor.start()
    if power_monitor.enabled:
        print("Power monitor active: sampling INA745B board draw.")
        if power_monitor.csv_path:
            print(f"Power log: {power_monitor.csv_path}")
        elif power_monitor.csv_error:
            print(f"Power log unavailable: {power_monitor.csv_error}")
    else:
        print(f"Power monitor inactive: {power_monitor.reason}")


@app.on_event("shutdown")
def shutdown_local_camera_proxy() -> None:
    local_camera_proxy.stop()
    power_monitor.stop()
    # Stop the breath pump on uvicorn's graceful shutdown path too, so it is
    # never left running after the service is stopped or restarted.
    try:
        service.breath_analyzer.shutdown()
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse, response_model=None)
def root() -> Response:
    if FRONTEND_INDEX.exists():
        return _frontend_response(FRONTEND_INDEX)
    return HTMLResponse(
        """
        <html>
            <head><title>Facial Attendance App</title></head>
            <body style="font-family:Segoe UI,Arial,sans-serif;padding:40px;background:#f5f7fa;">
                <h1>Frontend Not Built Yet</h1>
                <p>Run <code>npm install</code> and <code>npm run build</code> inside the <code>frontend</code> folder.</p>
            </body>
        </html>
        """
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- TEMPORARY TESTING: live board power draw --------------------------------
# Unauthenticated so the readout works in demo mode and can be polled with
# curl during bring-up. Remove this block when power profiling is finished.
@app.get("/api/v2/power")
def power_reading() -> dict:
    return power_monitor.snapshot()


@app.post("/api/v2/power/reset")
def power_reset() -> dict:
    power_monitor.reset_statistics()
    return power_monitor.snapshot()


SIMPLE_FRONTEND_INDEX = BASE_DIR / "simple_frontend" / "index.html"


@app.get("/simple", response_class=HTMLResponse, response_model=None)
def simple_frontend() -> Response:
    # Lightweight no-framework operator terminal. Shares the exact same
    # backend, auth, and recognition pipeline as the React app.
    if SIMPLE_FRONTEND_INDEX.exists():
        return _frontend_response(SIMPLE_FRONTEND_INDEX)
    return JSONResponse({"detail": "simple_frontend/index.html is missing."}, status_code=404)


DEMO_FRONTEND_INDEX = BASE_DIR / "demo_frontend" / "index.html"


@app.get("/demo", response_class=HTMLResponse, response_model=None)
def demo_frontend() -> Response:
    # DEMO build: minimal single-file UI for low-memory devices, no login.
    # Only reachable in a useful state when ATTENDANCE_DEMO_MODE is enabled.
    if DEMO_FRONTEND_INDEX.exists():
        return _frontend_response(DEMO_FRONTEND_INDEX)
    return JSONResponse({"detail": "demo_frontend/index.html is missing."}, status_code=404)


_LOGIN_LOCKOUT_MAX_FAILURES = int(os.getenv("ATTENDANCE_LOGIN_LOCKOUT_MAX_FAILURES", "5"))
_LOGIN_LOCKOUT_SECONDS = int(os.getenv("ATTENDANCE_LOGIN_LOCKOUT_SECONDS", "600"))
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()


def _login_lockout_remaining(username: str) -> int:
    now = time.monotonic()
    with _login_failures_lock:
        recent = [stamp for stamp in _login_failures.get(username, []) if now - stamp < _LOGIN_LOCKOUT_SECONDS]
        _login_failures[username] = recent
        if len(recent) >= _LOGIN_LOCKOUT_MAX_FAILURES:
            return int(_LOGIN_LOCKOUT_SECONDS - (now - recent[0])) + 1
    return 0


def _record_login_failure(username: str) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(username, []).append(time.monotonic())


def _clear_login_failures(username: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(username, None)


@app.post("/api/v2/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    username_key = payload.username.strip().lower()
    lockout_remaining = _login_lockout_remaining(username_key)
    if lockout_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many failed login attempts. "
                f"Try again in {max(1, (lockout_remaining + 59) // 60)} minute(s)."
            ),
        )
    if not authenticate_admin(payload.username, payload.password):
        _record_login_failure(username_key)
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    _clear_login_failures(username_key)
    session = session_store.create_session(payload.username)
    return LoginResponse(token=session.session_id, username=session.username, expires_at=session.expires_at)


@app.get("/api/v2/auth/status", response_model=AuthStatusResponse)
def auth_status() -> AuthStatusResponse:
    status = get_auth_status()
    return AuthStatusResponse(
        configured=status.configured,
        setup_required=status.setup_required,
        source=status.source,
        email_configured=status.email_configured,
        email_recovery_enabled=status.email_recovery_enabled,
        recovery_codes_available=status.recovery_codes_available,
    )


@app.post("/api/v2/auth/recovery-codes", response_model=RecoveryCodesResponse)
def generate_recovery_codes(
    payload: GenerateRecoveryCodesRequest,
    _: SessionState = Depends(require_auth),
) -> RecoveryCodesResponse:
    try:
        codes = generate_recovery_backup_codes(payload.current_password)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecoveryCodesResponse(codes=codes, remaining=len(codes))


@app.get("/api/v2/auth/recovery-codes/status")
def recovery_codes_status(_: SessionState = Depends(require_auth)) -> dict[str, int]:
    return {"remaining": recovery_backup_codes_remaining()}


@app.get("/api/v2/auth/email-settings")
def read_email_settings(_: SessionState = Depends(require_auth)) -> dict:
    settings = get_email_settings_public()
    settings["recovery_email"] = get_admin_email() or ""
    return settings


@app.put("/api/v2/auth/email-settings")
def update_email_settings(
    payload: EmailSettingsRequest,
    _: SessionState = Depends(require_auth),
) -> dict[str, str]:
    try:
        save_email_settings(
            current_password=payload.current_password,
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            from_email=payload.from_email,
            use_tls=payload.use_tls,
            use_ssl=payload.use_ssl,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "Email settings saved."}


@app.post("/api/v2/auth/email-settings/test")
def test_email_settings(_: SessionState = Depends(require_auth)) -> dict[str, str]:
    try:
        message = send_recovery_test_email()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": message}


@app.put("/api/v2/auth/recovery-email")
def update_recovery_email(
    payload: RecoveryEmailRequest,
    _: SessionState = Depends(require_auth),
) -> dict[str, str]:
    try:
        set_admin_recovery_email(payload.current_password, payload.email)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "Recovery email updated."}


@app.post("/api/v2/auth/reregister/request-code", response_model=RecoveryRequestResponse)
def request_reregister_verification(payload: RecoveryRequest) -> RecoveryRequestResponse:
    try:
        message = request_reregister_code(payload.email)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecoveryRequestResponse(ok=True, message=message)


@app.post("/api/v2/auth/reregister", response_model=LoginResponse)
def reregister_account(payload: ReregisterRequest) -> LoginResponse:
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    # Unauthenticated by design (sign-up screen); throttled like logins so
    # verification codes cannot be brute-forced.
    throttle_key = "__reregister__"
    lockout_remaining = _login_lockout_remaining(throttle_key)
    if lockout_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many attempts. "
                f"Try again in {max(1, (lockout_remaining + 59) // 60)} minute(s)."
            ),
        )
    try:
        reregister_admin(
            username=payload.username,
            password=payload.password,
            email=payload.email,
            code=payload.code,
        )
    except (RuntimeError, ValueError) as exc:
        _record_login_failure(throttle_key)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _clear_login_failures(throttle_key)
    session = session_store.create_session(payload.username.strip())
    return LoginResponse(token=session.session_id, username=session.username, expires_at=session.expires_at)


@app.post("/api/v2/auth/recovery-codes/reset-password", response_model=ResetCredentialsResponse)
def reset_password_with_recovery_code(payload: RecoveryCodeResetRequest) -> ResetCredentialsResponse:
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    # Unauthenticated by design (login screen), so throttle it exactly like
    # failed logins to block brute-force attempts on the code space.
    throttle_key = "__recovery_code__"
    lockout_remaining = _login_lockout_remaining(throttle_key)
    if lockout_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many recovery attempts. "
                f"Try again in {max(1, (lockout_remaining + 59) // 60)} minute(s)."
            ),
        )
    try:
        username = reset_admin_password_with_backup_code(payload.code, payload.new_password)
    except (RuntimeError, ValueError) as exc:
        _record_login_failure(throttle_key)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _clear_login_failures(throttle_key)
    return ResetCredentialsResponse(
        ok=True,
        username=username,
        message=f"Password reset successful. Log in as '{username}' with your new password.",
    )


@app.post("/api/v2/auth/setup", response_model=LoginResponse)
def setup_credentials(payload: SetupCredentialsRequest) -> LoginResponse:
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    try:
        setup_admin_credentials(username=payload.username, password=payload.password, email=payload.email)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = session_store.create_session(payload.username.strip())
    return LoginResponse(token=session.session_id, username=session.username, expires_at=session.expires_at)


@app.post("/api/v2/auth/reset", response_model=ResetCredentialsResponse)
def reset_credentials(payload: ResetCredentialsRequest) -> ResetCredentialsResponse:
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    try:
        reset_admin_credentials(
            username=payload.username,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    normalized_username = payload.username.strip()
    return ResetCredentialsResponse(
        ok=True,
        username=normalized_username,
        message="Password reset successful. Please log in with your username and new password.",
    )


@app.post("/api/v2/auth/recovery/username/request", response_model=RecoveryRequestResponse)
def request_username_recovery_code(payload: RecoveryRequest) -> RecoveryRequestResponse:
    try:
        message = request_username_recovery(payload.email)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecoveryRequestResponse(ok=True, message=message)


@app.post("/api/v2/auth/recovery/username/verify", response_model=UsernameRecoveryResponse)
def verify_username_recovery_code(payload: RecoveryVerifyRequest) -> UsernameRecoveryResponse:
    try:
        username = recover_username_by_email(payload.email, payload.code)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UsernameRecoveryResponse(
        ok=True,
        username=username,
        message="Username verified.",
    )


@app.post("/api/v2/auth/recovery/password/request", response_model=RecoveryRequestResponse)
def request_password_recovery_code(payload: RecoveryRequest) -> RecoveryRequestResponse:
    try:
        message = request_password_recovery(payload.email)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecoveryRequestResponse(ok=True, message=message)


@app.post("/api/v2/auth/recovery/password/verify", response_model=RecoveryRequestResponse)
def verify_password_recovery_code(payload: PasswordRecoveryVerifyRequest) -> RecoveryRequestResponse:
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    try:
        reset_admin_password_with_email(
            email=payload.email,
            code=payload.code,
            new_password=payload.new_password,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecoveryRequestResponse(
        ok=True,
        message="Password reset successful. Please log in with your new password.",
    )


@app.get("/api/v2/auth/me", response_model=MeResponse)
def me(session: SessionState = Depends(require_auth)) -> MeResponse:
    return MeResponse(username=session.username, expires_at=session.expires_at)


@app.post("/api/v2/auth/logout")
def logout(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    token = _extract_token(authorization, x_auth_token)
    if token:
        session_store.delete_session(token)
    return {"ok": True}


@app.get("/api/v2/auth/default-admin")
def default_admin_hint(session: SessionState = Depends(require_auth)) -> dict[str, str]:
    return {
        "username": get_admin_username() or "",
        "email": get_admin_email() or "",
        "configured": "true" if is_admin_auth_configured() else "false",
        "email_recovery_enabled": "true" if email_recovery_enabled() else "false",
    }


@app.get("/api/v2/status", response_model=ServiceStatus)
def status(_: SessionState = Depends(require_auth)) -> ServiceStatus:
    return service.status()


@app.get("/api/v2/architecture", response_model=ArchitectureNote)
def architecture(_: SessionState = Depends(require_auth)) -> ArchitectureNote:
    return service.architecture_note()


@app.get("/api/v2/workers", response_model=list[WorkerRead])
def list_workers(_: SessionState = Depends(require_auth)) -> list[WorkerRead]:
    return service.list_workers()


@app.post("/api/v2/workers/enroll", response_model=EnrollmentResult)
async def enroll_worker(
    employee_code: str = Form(...),
    name: str = Form(...),
    images: list[UploadFile] = File(...),
    replace_existing: bool = Form(True),
    _: SessionState = Depends(require_auth),
) -> EnrollmentResult:
    image_bytes = [await image.read() for image in images]
    try:
        return service.enroll_worker(
            employee_code=employee_code,
            name=name,
            image_bytes_list=image_bytes,
            replace_existing=replace_existing,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not enroll the employee: {exc}") from exc


@app.delete("/api/v2/workers/{employee_code}", response_model=DeleteWorkerResult)
def delete_worker(employee_code: str, _: SessionState = Depends(require_auth)) -> DeleteWorkerResult:
    try:
        return service.delete_worker(employee_code=employee_code)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not remove the employee: {exc}") from exc


@app.post("/api/v2/recognitions", response_model=RecognitionResult)
async def recognize(
    camera_id: str = Form(...),
    image: UploadFile = File(...),
    top_k: int = Form(3),
    _: SessionState = Depends(require_auth),
) -> RecognitionResult:
    image_bytes = await image.read()
    return service.recognize(image_bytes=image_bytes, camera_id=camera_id, top_k=top_k)


@app.post("/api/v2/breath-tests", response_model=BreathTestResult)
def run_breath_test(payload: BreathTestRequest, _: SessionState = Depends(require_auth)) -> BreathTestResult:
    try:
        return service.screen_worker(
            worker_id=payload.worker_id,
            camera_id=payload.camera_id,
            matched_score=payload.matched_score,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/breath-tests/start", response_model=BreathTestSessionStartResult)
def start_breath_test(
    payload: BreathTestStartRequest,
    _: SessionState = Depends(require_auth),
) -> BreathTestSessionStartResult:
    try:
        return service.start_breath_test(
            worker_id=payload.worker_id,
            camera_id=payload.camera_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/breath-tests/complete", response_model=BreathTestResult)
def complete_breath_test(
    payload: BreathTestCompleteRequest,
    _: SessionState = Depends(require_auth),
) -> BreathTestResult:
    try:
        return service.complete_breath_test(
            session_id=payload.session_id,
            matched_score=payload.matched_score,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/breath-checks/start", response_model=BreathCheckSessionStartResult)
def start_breath_check(
    payload: BreathCheckStartRequest,
    _: SessionState = Depends(require_auth),
) -> BreathCheckSessionStartResult:
    """Sensor-only measurement: nobody is identified and nothing is stored."""
    try:
        return service.start_breath_check(camera_id=payload.camera_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/breath-checks/complete", response_model=BreathCheckResult)
def complete_breath_check(
    payload: BreathCheckCompleteRequest,
    _: SessionState = Depends(require_auth),
) -> BreathCheckResult:
    try:
        return service.complete_breath_check(session_id=payload.session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Cancelling is identical for both kinds -- they share one session store.
@app.delete("/api/v2/breath-tests/{session_id}", response_model=BreathTestSessionCancelResult)
def cancel_breath_test(session_id: str, _: SessionState = Depends(require_auth)) -> BreathTestSessionCancelResult:
    try:
        return service.cancel_breath_test(session_id=session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/detections", response_model=DetectionResult)
async def detect(image: UploadFile = File(...), _: SessionState = Depends(require_auth)) -> DetectionResult:
    image_bytes = await image.read()
    return service.detect(image_bytes=image_bytes)


@app.get("/api/v2/local-camera/status", response_model=LocalCameraSessionResponse)
def local_camera_status(_: SessionState = Depends(require_auth)) -> LocalCameraSessionResponse:
    return LocalCameraSessionResponse(
        ok=True,
        running=local_camera_proxy.is_running(),
        mode="backend",
        source_name=local_camera_proxy.source_name(),
        frame_path="/api/v2/local-camera/frame",
    )


@app.post("/api/v2/local-camera/start", response_model=LocalCameraSessionResponse)
def start_local_camera(_: SessionState = Depends(require_auth)) -> LocalCameraSessionResponse:
    try:
        source_name = local_camera_proxy.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return LocalCameraSessionResponse(
        ok=True,
        running=True,
        mode="backend",
        source_name=source_name,
        frame_path="/api/v2/local-camera/frame",
    )


@app.post("/api/v2/local-camera/stop", response_model=LocalCameraStopResponse)
def stop_local_camera(_: SessionState = Depends(require_auth)) -> LocalCameraStopResponse:
    local_camera_proxy.stop()
    return LocalCameraStopResponse(ok=True, running=False)


@app.get("/api/v2/local-camera/frame", response_model=None)
def local_camera_frame(_: SessionState = Depends(require_camera_auth)) -> Response:
    try:
        if not local_camera_proxy.is_running():
            local_camera_proxy.start()
        frame_bytes = local_camera_proxy.get_frame_bytes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    response = Response(content=frame_bytes, media_type="image/jpeg")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/v2/local-camera/stream.mjpg", response_model=None)
def local_camera_stream(_: SessionState = Depends(require_camera_auth)) -> StreamingResponse:
    try:
        if not local_camera_proxy.is_running():
            local_camera_proxy.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    def generate() -> Iterator[bytes]:
        # Block for the NEXT frame rather than polling on a timer: the camera
        # thread paces us, so we never re-send a frame the client already has
        # and never spin faster than the sensor delivers.
        last_sequence: Optional[int] = None
        while True:
            try:
                frame_bytes, last_sequence = local_camera_proxy.get_frame(
                    timeout_seconds=5.0,
                    after_sequence=last_sequence,
                )
            except RuntimeError:
                break

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-store, no-cache, must-revalidate\r\n\r\n"
                + frame_bytes
                + b"\r\n"
            )

    response = StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/v2/attendance", response_model=list[AttendanceRow])
def list_attendance(
    limit: int = Query(default=100, ge=1, le=500),
    _: SessionState = Depends(require_auth),
) -> list[AttendanceRow]:
    return service.list_attendance(limit=limit)


@app.delete("/api/v2/attendance/{attendance_id}", response_model=DeleteAttendanceResult)
def delete_attendance(attendance_id: int, _: SessionState = Depends(require_auth)) -> DeleteAttendanceResult:
    return service.delete_attendance(attendance_id=attendance_id)


@app.post("/api/v2/index/rebuild", response_model=IndexStats)
def rebuild_index(_: SessionState = Depends(require_auth)) -> IndexStats:
    return service.rebuild_index()


@app.get("/{full_path:path}", response_model=None)
def spa_fallback(full_path: str) -> Response:
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if FRONTEND_INDEX.exists():
        return _frontend_response(FRONTEND_INDEX)
    return JSONResponse(
        {"detail": "Frontend not built. Run npm install && npm run build in the frontend folder."},
        status_code=404,
    )
