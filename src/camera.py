from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ImportError, ValueError):
    Gst = None

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
DEFAULT_RADXA_FRAMERATE = _int_env("ATTENDANCE_CAMERA_FRAMERATE", 60)
DEFAULT_APPSINK_NAME = "sink"


def _gst_is_available() -> bool:
    if Gst is None:
        return False

    Gst.init(None)
    return True


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


class GStreamerCameraStream:
    def __init__(self, pipeline: object, sink: object, source_name: str) -> None:
        self.pipeline = pipeline
        self.sink = sink
        self.source_name = source_name
        self.rotate_180 = _truthy_env("ATTENDANCE_CAMERA_ROTATE_180", False)
        self.flip_horizontal = _truthy_env("ATTENDANCE_CAMERA_FLIP_HORIZONTAL", False)

    def read(self) -> tuple[bool, object]:
        sample = self.sink.emit("try-pull-sample", Gst.SECOND)
        if sample is None:
            return False, None

        buffer = sample.get_buffer()
        if buffer is None:
            return False, None

        success, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not success:
            return False, None

        try:
            frame = self._frame_from_sample(sample, mapinfo.data)
            if self.rotate_180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            if self.flip_horizontal:
                frame = cv2.flip(frame, 1)
            return True, frame
        finally:
            buffer.unmap(mapinfo)

    def release(self) -> None:
        if self.pipeline is None:
            return

        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.sink = None

    def _frame_from_sample(self, sample: object, raw_bytes: bytes) -> np.ndarray:
        caps = sample.get_caps()
        width = DEFAULT_RADXA_WIDTH
        height = DEFAULT_RADXA_HEIGHT
        fmt = "I420"

        if caps is not None and caps.get_size() > 0:
            structure = caps.get_structure(0)
            if structure is not None:
                if structure.has_field("width"):
                    width = int(structure.get_value("width"))
                if structure.has_field("height"):
                    height = int(structure.get_value("height"))
                if structure.has_field("format"):
                    fmt = str(structure.get_value("format")).upper()

        data = np.frombuffer(raw_bytes, dtype=np.uint8)

        try:
            if fmt == "I420":
                yuv = data.reshape((height * 3 // 2, width))
                return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

            if fmt == "BGR":
                return data.reshape((height, width, 3)).copy()

            if fmt == "RGB":
                rgb = data.reshape((height, width, 3))
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

            if fmt in {"GRAY8", "GRAY"}:
                gray = data.reshape((height, width))
                return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        except (ValueError, cv2.error) as exc:
            raise RuntimeError(
                f"Could not decode {fmt} frame from {self.source_name} at {width}x{height}."
            ) from exc

        raise RuntimeError(f"Unsupported GStreamer frame format {fmt} from {self.source_name}.")


@dataclass(frozen=True)
class _CameraCandidate:
    source: object
    source_name: str
    api_preference: int | None = None
    decode_i420: bool = False
    prefer_native_gstreamer: bool = False


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
        f"appsink name={DEFAULT_APPSINK_NAME} max-buffers=1 drop=true sync=false"
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
                prefer_native_gstreamer=True,
            )
        ]
    else:
        radxa_candidates = [
            _CameraCandidate(
                source=_radxa_gstreamer_pipeline(device_path),
                source_name=f"Radxa GStreamer pipeline ({device_path})",
                api_preference=CAP_GSTREAMER,
                decode_i420=True,
                prefer_native_gstreamer=True,
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


def _open_gstreamer_camera(pipeline_str: str, source_name: str) -> GStreamerCameraStream | None:
    if not _gst_is_available():
        return None

    pipeline = Gst.parse_launch(pipeline_str)
    sink = pipeline.get_by_name(DEFAULT_APPSINK_NAME)
    if sink is None:
        sink = pipeline.get_by_name("appsink0")
    if sink is None:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("The pipeline did not expose an appsink named 'sink' or 'appsink0'.")

    state_change = pipeline.set_state(Gst.State.PLAYING)
    if state_change == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("GStreamer could not start the camera pipeline.")

    return GStreamerCameraStream(
        pipeline=pipeline,
        sink=sink,
        source_name=source_name,
    )


def open_camera() -> CameraStream | GStreamerCameraStream:
    attempted_sources: list[str] = []
    failure_details: list[str] = []

    for candidate in _camera_candidates():
        attempted_sources.append(candidate.source_name)
        if candidate.prefer_native_gstreamer and isinstance(candidate.source, str):
            try:
                native_camera = _open_gstreamer_camera(candidate.source, candidate.source_name)
            except Exception as exc:
                failure_details.append(f"{candidate.source_name}: native GStreamer failed ({exc})")
            else:
                if native_camera is not None:
                    return native_camera

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
    detail_suffix = f" Details: {' '.join(failure_details)}" if failure_details else ""
    raise RuntimeError(
        "Could not open webcam. "
        f"Tried {attempted}. "
        f"Detected Linux video devices: {', '.join(_linux_video_device_paths()) or 'none'}. "
        "You can set ATTENDANCE_CAMERA_BACKEND=radxa to force the pipeline or "
        "ATTENDANCE_CAMERA_DEVICE=/dev/videoX or ATTENDANCE_CAMERA_PIPELINE to override it."
        f"{detail_suffix}"
    )
