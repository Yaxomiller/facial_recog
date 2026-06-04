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
            patch("src.camera._radxa_device_paths", return_value=["/dev/video0"]),
            patch("src.camera._linux_video_device_paths", return_value=[]),
            patch("src.camera._open_gstreamer_camera", return_value=None) as open_gstreamer_camera,
            patch("src.camera.cv2.VideoCapture", side_effect=[direct_capture, fallback_capture]) as video_capture,
        ):
            camera = open_camera()

        self.assertIs(camera.capture, fallback_capture)
        self.assertTrue(camera.decode_i420)
        open_gstreamer_camera.assert_called_once()
        self.assertEqual(video_capture.call_args_list[0].args, (0,))
        self.assertEqual(video_capture.call_args_list[1].args[1], CAP_GSTREAMER)
        self.assertIn("v4l2src device=/dev/video0", video_capture.call_args_list[1].args[0])
        direct_capture.release.assert_called_once()

    def test_open_camera_honors_explicit_pipeline_and_raw_i420_flag(self) -> None:
        native_camera = Mock()
        native_camera.source_name = "Radxa GStreamer pipeline"
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
            patch("src.camera._open_gstreamer_camera", return_value=native_camera) as open_gstreamer_camera,
            patch("src.camera.cv2.VideoCapture") as video_capture,
        ):
            camera = open_camera()

        self.assertIs(camera, native_camera)
        self.assertEqual(open_gstreamer_camera.call_args.args, (pipeline, "Radxa GStreamer pipeline"))
        video_capture.assert_not_called()

    def test_open_camera_prefers_explicit_pipeline_in_auto_mode(self) -> None:
        native_camera = Mock()
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
            patch("src.camera._open_gstreamer_camera", return_value=native_camera) as open_gstreamer_camera,
            patch("src.camera.cv2.VideoCapture") as video_capture,
        ):
            camera = open_camera()

        self.assertIs(camera, native_camera)
        self.assertEqual(open_gstreamer_camera.call_args.args, (pipeline, "Radxa GStreamer pipeline"))
        video_capture.assert_not_called()

    def test_open_camera_tries_discovered_linux_video_devices(self) -> None:
        direct_capture = Mock()
        direct_capture.isOpened.return_value = False
        first_pipeline_capture = Mock()
        first_pipeline_capture.isOpened.return_value = False
        second_pipeline_capture = Mock()
        second_pipeline_capture.isOpened.return_value = True

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
            patch("src.camera._radxa_device_paths", return_value=["/dev/video11", "/dev/video12"]),
            patch("src.camera._open_gstreamer_camera", return_value=None) as open_gstreamer_camera,
            patch(
                "src.camera.cv2.VideoCapture",
                side_effect=[direct_capture, first_pipeline_capture, second_pipeline_capture],
            ) as video_capture,
        ):
            camera = open_camera()

        self.assertEqual(camera.source_name, "Radxa GStreamer pipeline (/dev/video12)")
        self.assertEqual(open_gstreamer_camera.call_count, 2)
        self.assertEqual(video_capture.call_args_list[1].args[1], CAP_GSTREAMER)
        self.assertIn("device=/dev/video11", video_capture.call_args_list[1].args[0])
        self.assertIn("device=/dev/video12", video_capture.call_args_list[2].args[0])

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
            patch("src.camera._radxa_device_paths", return_value=["/dev/video11"]),
            patch("src.camera._linux_video_device_paths", return_value=["/dev/video11"]),
            patch("src.camera._open_gstreamer_camera", return_value=None),
            patch("src.camera.cv2.VideoCapture", side_effect=[direct_capture, fallback_capture]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Detected Linux video devices: /dev/video11"):
                open_camera()


if __name__ == "__main__":
    unittest.main()
