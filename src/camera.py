from __future__ import annotations

import os
from typing import Optional

import cv2
import numpy as np

from src.config import CAMERA_INDEX


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value.strip())
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return float(value.strip())
    except ValueError:
        return default


def _rotate_enabled() -> bool:
    value = os.getenv("ATTENDANCE_CAMERA_ROTATE_180", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _flip_code() -> Optional[int]:
    value = os.getenv("ATTENDANCE_CAMERA_FLIP_CODE", "1").strip().lower()
    if not value or value in {"none", "off", "disable", "disabled"}:
        return None

    try:
        return int(value)
    except ValueError:
        return 1


DEFAULT_DEVICE = f"/dev/video{CAMERA_INDEX}"


def _default_device_path() -> str:
    return os.getenv("ATTENDANCE_CAMERA_DEVICE", DEFAULT_DEVICE).strip() or DEFAULT_DEVICE


def _default_width() -> int:
    return _int_env("ATTENDANCE_CAMERA_WIDTH", 1280)


def _default_height() -> int:
    return _int_env("ATTENDANCE_CAMERA_HEIGHT", 720)


def _default_framerate() -> int:
    return _int_env("ATTENDANCE_CAMERA_FRAMERATE", 30)


def _default_timeout_seconds() -> float:
    return max(0.1, _float_env("ATTENDANCE_CAMERA_TIMEOUT_SECONDS", 6.0))


def _default_rotate_code() -> Optional[int]:
    return cv2.ROTATE_180 if _rotate_enabled() else None


def _default_flip_code() -> Optional[int]:
    return _flip_code()

_GST = None


def _load_gst():
    global _GST
    if _GST is not None:
        return _GST

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            "The camera backend now uses GStreamer directly.\n"
            "On Radxa/Debian install: sudo apt install python3-gi gir1.2-gstreamer-1.0"
        ) from exc

    Gst.init(None)
    _GST = Gst
    return Gst


def _saturation_element() -> str:
    raw = os.getenv("ATTENDANCE_CAMERA_SATURATION", "").strip()
    if not raw:
        return ""
    try:
        saturation = max(0.0, min(2.0, float(raw)))
    except ValueError:
        return ""
    return f"videobalance saturation={saturation} ! "


def _build_pipeline_candidates(device_path: str, width: int, height: int, framerate: int) -> list[str]:
    configured_pipeline = os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()
    if configured_pipeline:
        return [configured_pipeline]

    # The sensor free-runs at up to 120fps; drop to the target rate BEFORE
    # videoconvert so the CPU only converts frames we actually use. sync=false
    # delivers frames as soon as they are ready instead of pacing them against
    # the pipeline clock, which keeps the preview latency low.
    tail = (
        f"videorate drop-only=true max-rate={max(1, framerate)} ! "
        + _saturation_element()
        + "videoconvert ! video/x-raw,format=BGR ! "
        "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    return [
        # en-awisp=1 en-largemode=0 enables the Allwinner ISP path on the Radxa
        # Cubie's patched v4l2src — without it the sensor never delivers frames.
        # The ISP outputs I420; videoconvert repacks to tight BGR for OpenCV.
        (
            f"v4l2src device={device_path} en-awisp=1 en-largemode=0 ! "
            f"video/x-raw,format=I420,width={width},height={height} ! {tail}"
        ),
        # Plain fallback for stock v4l2src builds (no Allwinner ISP properties).
        (
            f"v4l2src device={device_path} ! "
            f"video/x-raw,width={width},height={height} ! {tail}"
        ),
    ]


class CameraStream:
    def __init__(
        self,
        *,
        device_path: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        framerate: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        rotate_code: Optional[int] = None,
        flip_code: Optional[int] = None,
    ) -> None:
        resolved_device_path = device_path or _default_device_path()
        resolved_width = width if width is not None else _default_width()
        resolved_height = height if height is not None else _default_height()
        resolved_framerate = framerate if framerate is not None else _default_framerate()
        resolved_timeout_seconds = timeout_seconds if timeout_seconds is not None else _default_timeout_seconds()
        resolved_rotate_code = rotate_code if rotate_code is not None else _default_rotate_code()
        resolved_flip_code = flip_code if flip_code is not None else _default_flip_code()

        self.device_path = resolved_device_path
        self.width = resolved_width
        self.height = resolved_height
        self.framerate = resolved_framerate
        self.timeout_seconds = max(0.1, resolved_timeout_seconds)
        self.rotate_code = resolved_rotate_code
        self.flip_code = resolved_flip_code
        self.pipeline_candidates = _build_pipeline_candidates(
            resolved_device_path,
            resolved_width,
            resolved_height,
            resolved_framerate,
        )
        self.pipeline_description = self.pipeline_candidates[0]
        self.source_name = (
            "Configured GStreamer pipeline"
            if os.getenv("ATTENDANCE_CAMERA_PIPELINE", "").strip()
            else f"GStreamer pipeline ({resolved_device_path})"
        )
        self._gst = None
        self._pipeline = None
        self._sink = None

    def start(self) -> CameraStream:
        if self._pipeline is not None and self._sink is not None:
            return self

        gst = _load_gst()
        last_error: Optional[Exception] = None
        for description in self.pipeline_candidates:
            try:
                pipeline = gst.parse_launch(description)
            except Exception as exc:
                # e.g. en-awisp property missing on stock v4l2src builds —
                # fall through to the next candidate pipeline.
                last_error = exc
                continue

            sink = pipeline.get_by_name("sink")
            if sink is None:
                pipeline.set_state(gst.State.NULL)
                last_error = RuntimeError(
                    "The configured camera pipeline does not expose an appsink named 'sink'."
                )
                continue

            pipeline.set_state(gst.State.PLAYING)
            # The Allwinner ISP takes a couple of seconds to initialise on
            # first start. Block until the pipeline actually reaches PLAYING
            # so the first frame pull does not time out before the sensor is
            # streaming.
            state_change, _current, _pending = pipeline.get_state(int(gst.SECOND * 10))
            if state_change == gst.StateChangeReturn.FAILURE:
                pipeline.set_state(gst.State.NULL)
                last_error = RuntimeError(
                    f"The camera pipeline for {self.source_name} failed to reach the PLAYING state."
                )
                continue

            self.pipeline_description = description
            self._gst = gst
            self._pipeline = pipeline
            self._sink = sink
            return self

        raise RuntimeError(
            f"Could not create the camera pipeline for {self.source_name}."
        ) from last_error

    def _pull_sample(self):
        if self._gst is None or self._sink is None:
            raise RuntimeError("Camera has not been started.")

        timeout_ns = max(1, int(self.timeout_seconds * int(self._gst.SECOND)))
        return self._sink.emit("try-pull-sample", timeout_ns)

    def get_frame(self):
        sample = self._pull_sample()
        if sample is None:
            return None

        buffer = sample.get_buffer()
        success, mapinfo = buffer.map(self._gst.MapFlags.READ)
        if not success:
            return None

        try:
            data = np.frombuffer(mapinfo.data, dtype=np.uint8)
            expected_size = self.height * self.width * 3
            if data.size != expected_size:
                raise RuntimeError(
                    f"Camera returned an unexpected frame size for {self.source_name}. "
                    f"Expected {expected_size} bytes ({self.width}x{self.height} BGR), received {data.size}."
                )

            decoded = data.reshape((self.height, self.width, 3))
            rotate_code = self.rotate_code
            flip_code = self.flip_code
            if rotate_code == cv2.ROTATE_180 and flip_code in (1, 0, -1):
                # rotate180 == flip(-1); fold it into the configured flip so
                # the frame is transformed in a single memory pass.
                rotate_code = None
                flip_code = {1: 0, 0: 1, -1: None}[flip_code]
            if rotate_code is not None:
                decoded = cv2.rotate(decoded, rotate_code)
            if flip_code is not None:
                decoded = cv2.flip(decoded, flip_code)
            return decoded
        finally:
            buffer.unmap(mapinfo)

    def read(self) -> tuple[bool, object]:
        frame = self.get_frame()
        if frame is None:
            return False, None
        return True, frame

    def stop(self) -> None:
        if self._pipeline is None or self._gst is None:
            self._sink = None
            return

        self._pipeline.set_state(self._gst.State.NULL)
        self._pipeline = None
        self._sink = None
        self._gst = None

    def release(self) -> None:
        self.stop()


def open_camera() -> CameraStream:
    camera = CameraStream()
    camera.start()
    return camera
