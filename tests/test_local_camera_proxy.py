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

    def test_start_raises_clear_error_when_remote_stream_open_fails(self) -> None:
        proxy = LocalCameraProxy()

        with patch("src.local_camera_proxy.urlopen", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                proxy.start()


if __name__ == "__main__":
    unittest.main()
