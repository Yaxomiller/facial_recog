from __future__ import annotations

import socket
import threading
import unittest
from unittest.mock import patch

from src.local_camera_proxy import LocalCameraProxy


class _FakeStreamResponse:
    def __init__(self) -> None:
        self._chunks = [
            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\x00\x01",
            b"\x02\x03\xff\xd9\r\n",
        ]
        self._released = threading.Event()

    def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        raise socket.timeout()

    def close(self) -> None:
        self._released.set()


class _FakeEncodedFrame:
    def __init__(self, frame_bytes: bytes) -> None:
        self._frame_bytes = frame_bytes

    def tobytes(self) -> bytes:
        return self._frame_bytes


class _FakeLocalCamera:
    def __init__(self) -> None:
        self.source_name = "Radxa GStreamer pipeline (/dev/video0)"
        self._released = False

    def read(self) -> tuple[bool, object]:
        return True, object()

    def release(self) -> None:
        self._released = True


class LocalCameraProxyTests(unittest.TestCase):
    def test_start_and_get_frame_bytes_uses_remote_stream(self) -> None:
        stream_response = _FakeStreamResponse()
        proxy = LocalCameraProxy(stream_url="http://127.0.0.1:5051/stream.mjpg")

        with patch("src.local_camera_proxy.urlopen", return_value=stream_response):
            source_name = proxy.start()
            frame = proxy.get_frame_bytes(timeout_seconds=0.5)
            proxy.stop()

        self.assertEqual(source_name, "http://127.0.0.1:5051/stream.mjpg")
        self.assertEqual(frame, b"\xff\xd8\x00\x01\x02\x03\xff\xd9")
        self.assertTrue(stream_response._released.is_set())

    def test_start_falls_back_to_local_camera_when_remote_stream_fails(self) -> None:
        proxy = LocalCameraProxy()
        local_camera = _FakeLocalCamera()

        with (
            patch("src.local_camera_proxy.urlopen", side_effect=RuntimeError("boom")),
            patch("src.local_camera_proxy.open_camera", return_value=local_camera) as open_camera,
            patch(
                "src.local_camera_proxy.cv2.imencode",
                return_value=(True, _FakeEncodedFrame(b"\xff\xd8local\xff\xd9")),
            ) as imencode,
        ):
            source_name = proxy.start()
            frame = proxy.get_frame_bytes(timeout_seconds=0.5)
            proxy.stop()

        self.assertEqual(source_name, "Radxa GStreamer pipeline (/dev/video0)")
        self.assertEqual(frame, b"\xff\xd8local\xff\xd9")
        self.assertTrue(local_camera._released)
        open_camera.assert_called_once_with()
        imencode.assert_called_once()

    def test_start_raises_clear_error_when_remote_and_local_capture_fail(self) -> None:
        proxy = LocalCameraProxy()

        with (
            patch("src.local_camera_proxy.urlopen", side_effect=RuntimeError("boom")),
            patch("src.local_camera_proxy.open_camera", side_effect=RuntimeError("no local camera")),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Remote camera stream error: boom.*Local camera capture error: no local camera",
            ):
                proxy.start()


if __name__ == "__main__":
    unittest.main()
