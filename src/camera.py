from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import cv2

from src.config import CAMERA_INDEX


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value.strip())
    except ValueError:
        return default


CAP_GSTREAMER = getattr(cv2, "CAP_GSTREAMER", 1800)
DEFAULT_RADXA_WIDTH = _int_env("ATTENDANCE_CAMERA_WIDTH", 1920)
DEFAULT_RADXA_HEIGHT = _int_env("ATTENDANCE_CAMERA_HEIGHT", 1080)
DEFAULT_RADXA_FRAMERATE = _int_env("ATTENDANCE_CAMERA_FRAMERATE", 30)


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


def _radxa_gstreamer_pipeline(device_path: str) -> str:
    return (
        f"v4l2src device={device_path} en-awisp=1 en-largemode=0 ! "
        f"video/x-raw,format=I420,width={DEFAULT_RADXA_WIDTH},height={DEFAULT_RADXA_HEIGHT},"
        f"framerate={DEFAULT_RADXA_FRAMERATE}/1 ! "
        "appsink drop=true sync=false"
    )


def _pipeline_uses_i420(pipeline: str) -> bool:
    normalized = pipeline.replace(" ", "").upper()
    return "FORMAT=I420" in normalized


def _linux_video_device_paths() -> list[str]:
    dev_root = Path("/dev")
    if not dev_root.exists():
        return []

    discovered: list[tuple[int, str]] = []
    for path in dev_root.glob("video*"):
        suffix = path.name.removeprefix("video")
        if suffix.isdigit():
            discovered.append((int(suffix), str(path)))

    return [path for _index, path in sorted(discovered, key=lambda item: item[0])]


def _radxa_device_paths() -> list[str]:
    configured = os.getenv("ATTENDANCE_CAMERA_DEVICE", "").strip()
    if configured:
        return [configured]

    default_path = f"/dev/video{CAMERA_INDEX}"
    discovered = _linux_video_device_paths()
    if not discovered:
        return [default_path]

    ordered = [default_path]
    ordered.extend(path for path in discovered if path != default_path)
    return ordered


def _camera_candidates() -> list[_CameraCandidate]:
    preferred_backend = os.getenv("ATTENDANCE_CAMERA_BACKEND", "auto").strip().lower() or "auto"
    pipeline_from_env = os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()

    direct_candidate = _CameraCandidate(
        source=CAMERA_INDEX,
        source_name=f"camera index {CAMERA_INDEX}",
    )
    radxa_candidates: list[_CameraCandidate]
    if pipeline_from_env:
        radxa_candidates = [
            _CameraCandidate(
                source=pipeline_from_env,
                source_name="Radxa GStreamer pipeline",
                api_preference=CAP_GSTREAMER,
                decode_i420=_truthy_env("ATTENDANCE_CAMERA_PIPELINE_RAW_I420", _pipeline_uses_i420(pipeline_from_env)),
            )
        ]
    else:
        radxa_candidates = [
            _CameraCandidate(
                source=_radxa_gstreamer_pipeline(device_path),
                source_name=f"Radxa GStreamer pipeline ({device_path})",
                api_preference=CAP_GSTREAMER,
                decode_i420=True,
            )
            for device_path in _radxa_device_paths()
        ]

    if preferred_backend in {"gstreamer", "radxa"}:
        return [*radxa_candidates, direct_candidate]
    if preferred_backend in {"direct", "index", "opencv"}:
        return [direct_candidate]
    if pipeline_from_env:
        return [*radxa_candidates, direct_candidate]
    return [direct_candidate, *radxa_candidates]


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
        f"Detected Linux video devices: {', '.join(_linux_video_device_paths()) or 'none'}. "
        "You can set ATTENDANCE_CAMERA_BACKEND=radxa to force the pipeline or "
        "ATTENDANCE_CAMERA_DEVICE=/dev/videoX or ATTENDANCE_CAMERA_PIPELINE to override it."
    )
