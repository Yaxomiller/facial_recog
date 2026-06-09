from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import os
from pathlib import Path
import time

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.auth import (
    authenticate_admin,
    email_recovery_enabled,
    ensure_admin_auth_config,
    get_admin_email,
    get_admin_username,
    get_auth_status,
    is_admin_auth_configured,
    recover_username_by_email,
    request_password_recovery,
    request_username_recovery,
    reset_admin_credentials,
    reset_admin_password_with_email,
    setup_admin_credentials,
)
from src.local_camera_proxy import LocalCameraProxy
from src.session_store import SessionState, get_session_store
from src.v2.config import (
    MAX_PROFILE_CENTROID_THRESHOLD,
    OPEN_SET_MIN_CENTROID_SCORE,
    SINGLE_PROFILE_MIN_CENTROID_SCORE,
)
from src.v2.schemas import (
    ArchitectureNote,
    AttendanceRow,
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


if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="assets")


def _extract_token(authorization: str | None, x_auth_token: str | None) -> str | None:
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


def require_auth(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
) -> SessionState:
    token = _extract_token(authorization, x_auth_token)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session = session_store.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


def require_camera_auth(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
) -> SessionState:
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


@app.on_event("shutdown")
def shutdown_local_camera_proxy() -> None:
    local_camera_proxy.stop()


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


@app.post("/api/v2/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    if not authenticate_admin(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
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
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
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
    return service.enroll_worker(
        employee_code=employee_code,
        name=name,
        image_bytes_list=image_bytes,
        replace_existing=replace_existing,
    )


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
        while True:
            try:
                frame_bytes = local_camera_proxy.get_frame_bytes(timeout_seconds=5.0)
            except RuntimeError:
                break

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-store, no-cache, must-revalidate\r\n\r\n"
                + frame_bytes
                + b"\r\n"
            )
            time.sleep(0.03)

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
