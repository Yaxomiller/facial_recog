from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import numpy as np

from src.local_camera_proxy import LocalCameraProxy


class _FakeCamera:
    def __init__(self) -> None:
        self.source_name = "fake-camera"
        self._released = threading.Event()

    def read(self) -> tuple[bool, np.ndarray]:
        return True, np.zeros((8, 8, 3), dtype=np.uint8)

    def release(self) -> None:
        self._released.set()


class LocalCameraProxyTests(unittest.TestCase):
    def test_start_and_get_frame_bytes_uses_open_camera_stream(self) -> None:
        camera = _FakeCamera()
        proxy = LocalCameraProxy()

        with (
            patch("src.local_camera_proxy.open_camera", return_value=camera),
            patch("src.local_camera_proxy.cv2.imencode", return_value=(True, np.asarray([1, 2, 3], dtype=np.uint8))),
        ):
            source_name = proxy.start()
            frame = proxy.get_frame_bytes(timeout_seconds=0.5)
            proxy.stop()

        self.assertEqual(source_name, "fake-camera")
        self.assertEqual(frame, b"\x01\x02\x03")
        self.assertTrue(camera._released.is_set())

    def test_start_raises_clear_error_when_camera_open_fails(self) -> None:
        proxy = LocalCameraProxy()

        with patch("src.local_camera_proxy.open_camera", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                proxy.start()


if __name__ == "__main__":
    unittest.main()
