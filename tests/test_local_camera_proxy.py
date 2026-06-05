from __future__ import annotations

import unittest
from unittest.mock import patch

from src.local_camera_proxy import LocalCameraProxy


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
    def test_start_and_get_frame_bytes_use_local_camera(self) -> None:
        proxy = LocalCameraProxy()
        local_camera = _FakeLocalCamera()

        with (
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

    def test_start_raises_clear_error_when_local_capture_fails(self) -> None:
        proxy = LocalCameraProxy()

        with patch("src.local_camera_proxy.open_camera", side_effect=RuntimeError("no local camera")):
            with self.assertRaisesRegex(RuntimeError, "no local camera"):
                proxy.start()


if __name__ == "__main__":
    unittest.main()
