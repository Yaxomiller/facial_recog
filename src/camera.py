from __future__ import annotations

import os
from dataclasses import dataclass

import cv2

from src.config import CAMERA_INDEX


DEFAULT_RADXA_WIDTH = 1920
DEFAULT_RADXA_HEIGHT = 1080
DEFAULT_RADXA_FRAMERATE = 30
CAP_GSTREAMER = getattr(cv2, "CAP_GSTREAMER", 1800)


@dataclass
class CameraStream:
    capture: object
    source_name: str
    decode_i420: bool = False

    def read(self) -> tuple[bool, object]:
        ok, frame = self.capture.read()
        if not ok:
            return False, frame

        if self.decode_i420:
            try:
                frame = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
            except cv2.error as exc:
                raise RuntimeError(f"Could not decode I420 frame from {self.source_name}.") from exc

        return True, frame

    def release(self) -> None:
        self.capture.release()


@dataclass(frozen=True)
class _CameraCandidate:
    source: object
    source_name: str
    api_preference: int | None = None
    decode_i420: bool = False


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _radxa_device_path() -> str:
    configured = os.getenv("ATTENDANCE_CAMERA_DEVICE", "").strip()
    if configured:
        return configured
    return f"/dev/video{CAMERA_INDEX}"


def _radxa_gstreamer_pipeline() -> str:
    configured = os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()
    if configured:
        return configured

    return (
        f"v4l2src device={_radxa_device_path()} en-awisp=1 en-largemode=0 ! "
        f"video/x-raw,format=I420,width={DEFAULT_RADXA_WIDTH},height={DEFAULT_RADXA_HEIGHT},"
        f"framerate={DEFAULT_RADXA_FRAMERATE}/1 ! appsink drop=true sync=false"
    )


def _pipeline_uses_i420(pipeline: str) -> bool:
    normalized = pipeline.replace(" ", "").upper()
    return "FORMAT=I420" in normalized


def _camera_candidates() -> list[_CameraCandidate]:
    preferred_backend = os.getenv("ATTENDANCE_CAMERA_BACKEND", "auto").strip().lower() or "auto"
    pipeline_from_env = os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()

    direct_candidate = _CameraCandidate(
        source=CAMERA_INDEX,
        source_name=f"camera index {CAMERA_INDEX}",
    )
    radxa_candidate = _CameraCandidate(
        source=_radxa_gstreamer_pipeline(),
        source_name="Radxa GStreamer pipeline",
        api_preference=CAP_GSTREAMER,
        decode_i420=(
            True
            if not pipeline_from_env
            else _truthy_env("ATTENDANCE_CAMERA_PIPELINE_RAW_I420", _pipeline_uses_i420(pipeline_from_env))
        ),
    )

    if preferred_backend in {"gstreamer", "radxa"}:
        return [radxa_candidate, direct_candidate]
    if preferred_backend in {"direct", "index", "opencv"}:
        return [direct_candidate]
    if pipeline_from_env:
        return [radxa_candidate, direct_candidate]
    return [direct_candidate, radxa_candidate]


def open_camera() -> CameraStream:
    attempted_sources: list[str] = []

    for candidate in _camera_candidates():
        attempted_sources.append(candidate.source_name)
        if candidate.api_preference is None:
            capture = cv2.VideoCapture(candidate.source)
        else:
            capture = cv2.VideoCapture(candidate.source, candidate.api_preference)

        if capture.isOpened():
            return CameraStream(
                capture=capture,
                source_name=candidate.source_name,
                decode_i420=candidate.decode_i420,
            )

        capture.release()

    attempted = ", ".join(attempted_sources)
    raise RuntimeError(
        "Could not open webcam. "
        f"Tried {attempted}. "
        "You can set ATTENDANCE_CAMERA_BACKEND=radxa to force the pipeline or "
        "ATTENDANCE_CAMERA_PIPELINE to override it."
    )
