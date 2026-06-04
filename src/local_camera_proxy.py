from __future__ import annotations

import os
import socket
import threading
import time
from urllib.request import Request, urlopen

import cv2

from src.camera import open_camera

STARTUP_TIMEOUT_SECONDS = max(0.5, float(os.getenv("ATTENDANCE_LOCAL_CAMERA_STARTUP_TIMEOUT_SECONDS", "4.0")))
REMOTE_CAMERA_STREAM_URL = (
    os.getenv("ATTENDANCE_REMOTE_CAMERA_STREAM_URL", "http://127.0.0.1:5051/stream.mjpg").strip()
    or "http://127.0.0.1:5051/stream.mjpg"
)
STREAM_READ_TIMEOUT_SECONDS = max(0.25, float(os.getenv("ATTENDANCE_REMOTE_CAMERA_READ_TIMEOUT_SECONDS", "1.0")))
STREAM_CHUNK_SIZE = max(1024, int(os.getenv("ATTENDANCE_REMOTE_CAMERA_CHUNK_SIZE", "4096")))
STREAM_BUFFER_LIMIT = max(32768, int(os.getenv("ATTENDANCE_REMOTE_CAMERA_BUFFER_LIMIT", "1048576")))
LOCAL_CAMERA_FRAME_INTERVAL_SECONDS = max(
    0.01,
    float(os.getenv("ATTENDANCE_LOCAL_CAMERA_FRAME_INTERVAL_SECONDS", "0.03")),
)
LOCAL_CAMERA_JPEG_QUALITY = min(
    100,
    max(40, int(os.getenv("ATTENDANCE_LOCAL_CAMERA_JPEG_QUALITY", "85"))),
)


class LocalCameraProxy:
    def __init__(self, stream_url: str = REMOTE_CAMERA_STREAM_URL) -> None:
        self._stream_url = stream_url
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._latest_jpeg: bytes | None = None
        self._source_name = ""
        self._last_error = ""

    def start(self) -> str:
        with self._condition:
            if self._thread is None or not self._thread.is_alive():
                self._latest_jpeg = None
                self._source_name = ""
                self._last_error = ""
                self._stop_event = threading.Event()
                self._thread = threading.Thread(
                    target=self._capture_loop,
                    args=(self._stop_event,),
                    daemon=True,
                    name="local-camera-proxy",
                )
                self._thread.start()

            return self._wait_for_first_frame_locked(STARTUP_TIMEOUT_SECONDS)

    def stop(self) -> None:
        with self._condition:
            stop_event = self._stop_event
            thread = self._thread
            self._stop_event = None
            self._thread = None

        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        with self._condition:
            self._latest_jpeg = None
            self._source_name = ""
            self._last_error = ""
            self._condition.notify_all()

    def is_running(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def source_name(self) -> str:
        with self._condition:
            return self._source_name

    def get_frame_bytes(self, timeout_seconds: float = 2.0) -> bytes:
        with self._condition:
            deadline = time.monotonic() + timeout_seconds
            while self._latest_jpeg is None:
                if self._last_error:
                    raise RuntimeError(self._last_error)
                if self._thread is None or not self._thread.is_alive():
                    raise RuntimeError("Local camera is not running.")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Timed out waiting for the local camera frame.")
                self._condition.wait(timeout=min(0.25, remaining))

            return self._latest_jpeg

    def _wait_for_first_frame_locked(self, timeout_seconds: float) -> str:
        deadline = time.monotonic() + timeout_seconds
        while self._latest_jpeg is None:
            if self._last_error:
                raise RuntimeError(self._last_error)
            if self._thread is None or not self._thread.is_alive():
                raise RuntimeError("Could not start the local camera bridge.")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Timed out waiting for the local camera bridge to deliver the first frame.")
            self._condition.wait(timeout=min(0.25, remaining))

        return self._source_name or "local-camera"

    def _capture_loop(self, stop_event: threading.Event) -> None:
        errors: list[str] = []
        capture_succeeded = False
        try:
            if self._stream_url:
                try:
                    self._capture_remote_stream(stop_event)
                    capture_succeeded = True
                    return
                except Exception as exc:
                    errors.append(f"Remote camera stream error: {exc}")

            try:
                self._capture_local_camera(stop_event)
                capture_succeeded = True
            except Exception as exc:
                errors.append(f"Local camera capture error: {exc}")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if errors and not capture_succeeded:
                with self._condition:
                    self._last_error = " ".join(errors)
                    self._condition.notify_all()

            with self._condition:
                if self._thread is threading.current_thread():
                    self._thread = None
                if self._stop_event is stop_event:
                    self._stop_event = None
                self._condition.notify_all()

    def _capture_remote_stream(self, stop_event: threading.Event) -> None:
        stream_response = None
        buffer = bytearray()
        received_frame = False
        first_frame_deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        try:
            request = Request(self._stream_url, headers={"Accept": "multipart/x-mixed-replace,image/jpeg"})
            stream_response = urlopen(request, timeout=STREAM_READ_TIMEOUT_SECONDS)
            with self._condition:
                self._source_name = self._stream_url
                self._condition.notify_all()

            while not stop_event.is_set():
                try:
                    chunk = stream_response.read(STREAM_CHUNK_SIZE)
                except socket.timeout:
                    if not received_frame and time.monotonic() >= first_frame_deadline:
                        raise RuntimeError(f"Timed out waiting for frames from {self._stream_url}")
                    continue

                if not chunk:
                    raise RuntimeError(f"Remote camera stream closed: {self._stream_url}")

                buffer.extend(chunk)
                if len(buffer) > STREAM_BUFFER_LIMIT:
                    del buffer[:-STREAM_BUFFER_LIMIT]

                frame_bytes = _extract_jpeg_frame(buffer)
                if frame_bytes is None:
                    continue

                received_frame = True
                with self._condition:
                    self._latest_jpeg = frame_bytes
                    self._condition.notify_all()

                stop_event.wait(0.01)
        finally:
            if stream_response is not None:
                stream_response.close()

    def _capture_local_camera(self, stop_event: threading.Event) -> None:
        camera = open_camera()
        try:
            with self._condition:
                self._source_name = camera.source_name
                self._condition.notify_all()

            while not stop_event.is_set():
                ok, frame = camera.read()
                if not ok:
                    stop_event.wait(LOCAL_CAMERA_FRAME_INTERVAL_SECONDS)
                    continue

                encoded_ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, LOCAL_CAMERA_JPEG_QUALITY],
                )
                if not encoded_ok:
                    stop_event.wait(LOCAL_CAMERA_FRAME_INTERVAL_SECONDS)
                    continue

                with self._condition:
                    self._latest_jpeg = encoded.tobytes()
                    self._condition.notify_all()

                stop_event.wait(LOCAL_CAMERA_FRAME_INTERVAL_SECONDS)
        finally:
            camera.release()


def _extract_jpeg_frame(buffer: bytearray) -> bytes | None:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        return None

    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
        if start > 0:
            del buffer[:start]
        return None

    frame = bytes(buffer[start : end + 2])
    del buffer[: end + 2]
    return frame
