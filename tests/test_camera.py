from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

import numpy as np

from src.camera import CAP_GSTREAMER, CameraStream, open_camera


class CameraTests(unittest.TestCase):
    def test_open_camera_falls_back_to_radxa_pipeline_when_direct_open_fails(self) -> None:
        direct_capture = Mock()
        direct_capture.isOpened.return_value = False
        fallback_capture = Mock()
        fallback_capture.isOpened.return_value = True

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_BACKEND": "auto",
                    "ATTENDANCE_CAMERA_PIPELINE": "",
                    "ATTENDANCE_CAMERA_DEVICE": "",
                },
                clear=False,
            ),
            patch("src.camera.cv2.VideoCapture", side_effect=[direct_capture, fallback_capture]) as video_capture,
        ):
            camera = open_camera()

        self.assertIs(camera.capture, fallback_capture)
        self.assertTrue(camera.decode_i420)
        self.assertEqual(video_capture.call_args_list[0].args, (0,))
        self.assertEqual(video_capture.call_args_list[1].args[1], CAP_GSTREAMER)
        self.assertIn("v4l2src device=/dev/video0", video_capture.call_args_list[1].args[0])
        direct_capture.release.assert_called_once()

    def test_open_camera_honors_explicit_pipeline_and_raw_i420_flag(self) -> None:
        pipeline_capture = Mock()
        pipeline_capture.isOpened.return_value = True
        pipeline = "v4l2src device=/dev/video2 ! video/x-raw,format=I420 ! appsink"

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_BACKEND": "radxa",
                    "ATTENDANCE_CAMERA_PIPELINE": pipeline,
                    "ATTENDANCE_CAMERA_PIPELINE_RAW_I420": "true",
                },
                clear=False,
            ),
            patch("src.camera.cv2.VideoCapture", return_value=pipeline_capture) as video_capture,
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, "Radxa GStreamer pipeline")
        self.assertTrue(camera.decode_i420)
        self.assertEqual(video_capture.call_args.args, (pipeline, CAP_GSTREAMER))

    def test_open_camera_prefers_explicit_pipeline_in_auto_mode(self) -> None:
        pipeline_capture = Mock()
        pipeline_capture.isOpened.return_value = True
        pipeline = "v4l2src device=/dev/video1 ! video/x-raw, format=I420 ! appsink"

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_BACKEND": "auto",
                    "ATTENDANCE_CAMERA_PIPELINE": pipeline,
                },
                clear=False,
            ),
            patch("src.camera.cv2.VideoCapture", return_value=pipeline_capture) as video_capture,
        ):
            camera = open_camera()

        self.assertTrue(camera.decode_i420)
        self.assertEqual(video_capture.call_args.args, (pipeline, CAP_GSTREAMER))

    def test_read_decodes_i420_frame_into_bgr(self) -> None:
        raw_frame = np.zeros((12, 8), dtype=np.uint8)
        decoded_frame = np.zeros((8, 8, 3), dtype=np.uint8)
        capture = Mock()
        capture.read.return_value = (True, raw_frame)
        camera = CameraStream(capture=capture, source_name="Radxa GStreamer pipeline", decode_i420=True)

        with patch("src.camera.cv2.cvtColor", return_value=decoded_frame) as cvt_color:
            ok, frame = camera.read()

        self.assertTrue(ok)
        self.assertIs(frame, decoded_frame)
        cvt_color.assert_called_once()

    def test_open_camera_raises_clear_error_when_all_attempts_fail(self) -> None:
        direct_capture = Mock()
        direct_capture.isOpened.return_value = False
        fallback_capture = Mock()
        fallback_capture.isOpened.return_value = False

        with (
            patch.dict(
                os.environ,
                {
                    "ATTENDANCE_CAMERA_BACKEND": "auto",
                    "ATTENDANCE_CAMERA_PIPELINE": "",
                },
                clear=False,
            ),
            patch("src.camera.cv2.VideoCapture", side_effect=[direct_capture, fallback_capture]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Tried camera index 0, Radxa GStreamer pipeline"):
                open_camera()


if __name__ == "__main__":
    unittest.main()
