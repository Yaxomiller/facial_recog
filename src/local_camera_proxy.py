from __future__ import annotations

import os
import threading
import time

import cv2

from src.camera import CameraStream, open_camera


JPEG_QUALITY = max(40, min(100, int(os.getenv("ATTENDANCE_LOCAL_CAMERA_JPEG_QUALITY", "88"))))
STARTUP_TIMEOUT_SECONDS = max(0.5, float(os.getenv("ATTENDANCE_LOCAL_CAMERA_STARTUP_TIMEOUT_SECONDS", "4.0")))


class LocalCameraProxy:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._camera: CameraStream | None = None
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
            self._camera = None
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
        camera: CameraStream | None = None
        try:
            camera = open_camera()
            with self._condition:
                self._camera = camera
                self._source_name = camera.source_name
                self._condition.notify_all()

            while not stop_event.is_set():
                ok, frame = camera.read()
                if not ok:
                    raise RuntimeError("Could not read frame from webcam.")

                encoded_ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
                )
                if not encoded_ok:
                    raise RuntimeError("Could not encode webcam frame.")

                with self._condition:
                    self._latest_jpeg = encoded.tobytes()
                    self._condition.notify_all()

                stop_event.wait(0.01)
        except Exception as exc:
            with self._condition:
                self._last_error = str(exc)
                self._condition.notify_all()
        finally:
            if camera is not None:
                camera.release()

            with self._condition:
                if self._camera is camera:
                    self._camera = None
                if self._thread is threading.current_thread():
                    self._thread = None
                if self._stop_event is stop_event:
                    self._stop_event = None
                self._condition.notify_all()
