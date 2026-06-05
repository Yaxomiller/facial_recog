from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from src.camera import CAP_GSTREAMER, CameraStream, open_camera
from src.config import CAMERA_INDEX


def _frame() -> np.ndarray:
    return np.zeros((8, 8, 3), dtype=np.uint8)


class CameraTests(unittest.TestCase):
    def test_open_camera_uses_configured_pipeline_before_other_sources(self) -> None:
        pipeline = "v4l2src device=/dev/video2 ! video/x-raw,format=I420 ! appsink"
        configured_capture = Mock()
        configured_capture.isOpened.return_value = True
        configured_capture.read.return_value = (True, _frame())

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_PIPELINE": pipeline,
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera._camera_device_paths", return_value=["/dev/video0"]),
            patch("src.camera.cv2.VideoCapture", return_value=configured_capture) as video_capture,
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, "Configured camera pipeline")
        self.assertEqual(camera.decode_color_code, cv2.COLOR_YUV2BGR_I420)
        self.assertEqual(video_capture.call_args.args, (pipeline, CAP_GSTREAMER))

    def test_open_camera_prefers_radxa_pipeline_before_direct_camera(self) -> None:
        pipeline_capture = Mock()
        pipeline_capture.isOpened.return_value = True
        pipeline_capture.read.return_value = (True, _frame())

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_PIPELINE": "",
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera._camera_device_paths", return_value=["/dev/video0"]),
            patch("src.camera.cv2.VideoCapture", return_value=pipeline_capture) as video_capture,
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, "Radxa GStreamer pipeline (/dev/video0, NV12)")
        self.assertEqual(video_capture.call_args.args[1], CAP_GSTREAMER)
        self.assertIn("device=/dev/video0", video_capture.call_args.args[0])
        self.assertIn("format=NV12", video_capture.call_args.args[0])

    def test_open_camera_falls_back_from_nv12_to_i420_for_radxa_pipeline(self) -> None:
        empty_nv12_capture = Mock()
        empty_nv12_capture.isOpened.return_value = True
        empty_nv12_capture.read.return_value = (False, None)
        i420_capture = Mock()
        i420_capture.isOpened.return_value = True
        i420_capture.read.return_value = (True, _frame())

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_PIPELINE": "",
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera._camera_device_paths", return_value=["/dev/video0"]),
            patch("src.camera.cv2.VideoCapture", side_effect=[empty_nv12_capture, i420_capture]),
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, "Radxa GStreamer pipeline (/dev/video0, I420)")
        empty_nv12_capture.release.assert_called_once()

    def test_open_camera_skips_open_capture_that_returns_no_frame(self) -> None:
        pipeline = "v4l2src device=/dev/video2 ! video/x-raw,format=I420 ! appsink"
        empty_capture = Mock()
        empty_capture.isOpened.return_value = True
        empty_capture.read.return_value = (False, None)
        direct_capture = Mock()
        direct_capture.isOpened.return_value = True
        direct_capture.read.return_value = (True, _frame())

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_PIPELINE": pipeline,
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera._camera_device_paths", return_value=[]),
            patch("src.camera.cv2.VideoCapture", side_effect=[empty_capture, direct_capture]),
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, f"camera index {CAMERA_INDEX}")
        empty_capture.release.assert_called_once()

    def test_read_decodes_i420_frame_into_bgr(self) -> None:
        raw_frame = np.zeros((12, 8), dtype=np.uint8)
        decoded_frame = np.zeros((8, 8, 3), dtype=np.uint8)
        capture = Mock()
        camera = CameraStream(
            capture=capture,
            source_name="Radxa GStreamer pipeline",
            decode_color_code=cv2.COLOR_YUV2BGR_I420,
            pending_frame=raw_frame,
        )

        with patch("src.camera.cv2.cvtColor", return_value=decoded_frame) as cvt_color:
            ok, frame = camera.read()

        self.assertTrue(ok)
        self.assertIs(frame, decoded_frame)
        cvt_color.assert_called_once()

    def test_read_decodes_nv12_frame_into_bgr(self) -> None:
        raw_frame = np.zeros((12, 8), dtype=np.uint8)
        decoded_frame = np.zeros((8, 8, 3), dtype=np.uint8)
        capture = Mock()
        camera = CameraStream(
            capture=capture,
            source_name="Radxa GStreamer pipeline",
            decode_color_code=cv2.COLOR_YUV2BGR_NV12,
            pending_frame=raw_frame,
        )

        with patch("src.camera.cv2.cvtColor", return_value=decoded_frame) as cvt_color:
            ok, frame = camera.read()

        self.assertTrue(ok)
        self.assertIs(frame, decoded_frame)
        cvt_color.assert_called_once()

    def test_open_camera_raises_clear_error_when_all_attempts_fail(self) -> None:
        first_pipeline_capture = Mock()
        first_pipeline_capture.isOpened.return_value = False
        second_pipeline_capture = Mock()
        second_pipeline_capture.isOpened.return_value = False
        direct_capture = Mock()
        direct_capture.isOpened.return_value = False

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_PIPELINE": "",
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera._camera_device_paths", return_value=["/dev/video11"]),
            patch("src.camera._linux_video_device_paths", return_value=["/dev/video11"]),
            patch("src.camera.cv2.VideoCapture", side_effect=[first_pipeline_capture, second_pipeline_capture, direct_capture]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Detected Linux video devices: /dev/video11"):
                open_camera()


if __name__ == "__main__":
    unittest.main()
