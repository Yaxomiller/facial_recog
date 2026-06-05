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
    decode_color_code: int | None = None
    pending_frame: object | None = None

    def _decode_frame(self, frame: object) -> object:
        if self.decode_color_code is None:
            return frame

        try:
            return cv2.cvtColor(frame, self.decode_color_code)
        except cv2.error as exc:
            raise RuntimeError(f"Could not decode frame from {self.source_name}.") from exc

    def read(self) -> tuple[bool, object]:
        if self.pending_frame is not None:
            frame = self.pending_frame
            self.pending_frame = None
            return True, self._decode_frame(frame)

        ok, frame = self.capture.read()
        if not ok or frame is None:
            return False, frame

        return True, self._decode_frame(frame)

    def release(self) -> None:
        self.capture.release()


@dataclass(frozen=True)
class _CameraCandidate:
    source: object
    source_name: str
    api_preference: int | None = None
    decode_color_code: int | None = None


def _radxa_gstreamer_pipeline(device_path: str, pixel_format: str) -> str:
    return (
        f"v4l2src device={device_path} en-awisp=1 en-largemode=0 ! "
        f"video/x-raw,format={pixel_format},width={DEFAULT_RADXA_WIDTH},height={DEFAULT_RADXA_HEIGHT},"
        f"framerate={DEFAULT_RADXA_FRAMERATE}/1 ! "
        "appsink drop=true sync=false"
    )


def _pipeline_color_code(pipeline: str) -> int | None:
    normalized = pipeline.replace(" ", "").upper()
    if "FORMAT=NV12" in normalized:
        return cv2.COLOR_YUV2BGR_NV12
    if "FORMAT=I420" in normalized:
        return cv2.COLOR_YUV2BGR_I420
    return None


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


def _camera_device_paths() -> list[str]:
    configured = os.getenv("ATTENDANCE_CAMERA_DEVICE", "").strip()
    discovered = _linux_video_device_paths()
    if configured:
        ordered = [configured]
        ordered.extend(path for path in discovered if path != configured)
        return ordered

    if not Path("/dev").exists():
        return []

    default_path = f"/dev/video{CAMERA_INDEX}"
    if not discovered:
        return [default_path]
    ordered = [default_path]
    ordered.extend(path for path in discovered if path != default_path)
    return ordered


def _camera_candidates() -> list[_CameraCandidate]:
    pipeline_from_env = os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()

    candidates: list[_CameraCandidate] = []
    if pipeline_from_env:
        candidates.append(
            _CameraCandidate(
                source=pipeline_from_env,
                source_name="Configured camera pipeline",
                api_preference=CAP_GSTREAMER,
                decode_color_code=_pipeline_color_code(pipeline_from_env),
            )
        )

    candidates.extend(
        [
            _CameraCandidate(
                source=_radxa_gstreamer_pipeline(device_path, pixel_format),
                source_name=f"Radxa GStreamer pipeline ({device_path}, {pixel_format})",
                api_preference=CAP_GSTREAMER,
                decode_color_code=_pipeline_color_code(f"format={pixel_format}"),
            )
            for device_path in _camera_device_paths()
            for pixel_format in ("NV12", "I420")
        ]
    )
    candidates.append(
        _CameraCandidate(
            source=CAMERA_INDEX,
            source_name=f"camera index {CAMERA_INDEX}",
        )
    )
    return candidates


def _open_candidate(candidate: _CameraCandidate) -> CameraStream | None:
    if candidate.api_preference is None:
        capture = cv2.VideoCapture(candidate.source)
    else:
        capture = cv2.VideoCapture(candidate.source, candidate.api_preference)

    if not capture.isOpened():
        capture.release()
        return None

    ok, frame = capture.read()
    if not ok or frame is None:
        capture.release()
        return None

    return CameraStream(
        capture=capture,
        source_name=candidate.source_name,
        decode_color_code=candidate.decode_color_code,
        pending_frame=frame,
    )


def open_camera() -> CameraStream:
    attempted_sources: list[str] = []

    for candidate in _camera_candidates():
        attempted_sources.append(candidate.source_name)
        camera = _open_candidate(candidate)
        if camera is not None:
            return camera

    attempted = ", ".join(attempted_sources)
    raise RuntimeError(
        "Could not open webcam. "
        f"Tried {attempted}. "
        f"Detected Linux video devices: {', '.join(_linux_video_device_paths()) or 'none'}. "
        "You can set ATTENDANCE_CAMERA_DEVICE=/dev/videoX or ATTENDANCE_CAMERA_PIPELINE to override it."
    )
