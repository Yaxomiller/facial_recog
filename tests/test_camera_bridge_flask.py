from __future__ import annotations

import unittest
from unittest.mock import patch

import camera_bridge_flask


class _StubBridge:
    def __init__(self) -> None:
        self.running = False
        self.device_path = "/dev/video0"
        self.pipeline = "stub-pipeline"
        self.last_error = ""

    def status(self):
        return camera_bridge_flask.CameraStatus(
            running=self.running,
            device=self.device_path,
            pipeline=self.pipeline,
            last_error=self.last_error,
        )

    def open(self):
        self.running = True
        return self.status()

    def close(self):
        self.running = False
        return self.status()

    def read_jpeg(self) -> bytes:
        self.running = True
        return b"jpeg-bytes"

    def mjpeg_stream(self):
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\njpeg-bytes\r\n"


class FlaskCameraBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = _StubBridge()
        self.patch_bridge = patch.object(camera_bridge_flask, "camera_bridge", self.stub)
        self.patch_bridge.start()
        self.client = camera_bridge_flask.app.test_client()

    def tearDown(self) -> None:
        self.patch_bridge.stop()

    def test_status_reports_current_device(self) -> None:
        response = self.client.get("/camera/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["device"], "/dev/video0")
        self.assertFalse(payload["running"])

    def test_open_camera_endpoint_marks_bridge_running(self) -> None:
        response = self.client.post("/camera/open")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["running"])

    def test_single_frame_endpoint_returns_jpeg(self) -> None:
        response = self.client.get("/camera/frame.jpg")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"jpeg-bytes")


if __name__ == "__main__":
    unittest.main()
