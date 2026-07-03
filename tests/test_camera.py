from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from src.camera import CameraStream, open_camera


class _FakeMapInfo:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeBuffer:
    def __init__(self, data: bytes, *, map_success: bool = True) -> None:
        self._data = data
        self._map_success = map_success
        self.unmapped = False

    def map(self, _flags):
        return self._map_success, _FakeMapInfo(self._data)

    def unmap(self, _mapinfo) -> None:
        self.unmapped = True


class _FakeSample:
    def __init__(self, buffer: _FakeBuffer) -> None:
        self._buffer = buffer

    def get_buffer(self) -> _FakeBuffer:
        return self._buffer


class _FakeSink:
    def __init__(self, samples: list[Optional[_FakeSample]]) -> None:
        self._samples = list(samples)
        self.emit_calls: list[tuple[str, int]] = []

    def emit(self, signal: str, timeout_ns: int):
        self.emit_calls.append((signal, timeout_ns))
        if self._samples:
            return self._samples.pop(0)
        return None


class _FakePipeline:
    def __init__(self, sink: Optional[_FakeSink]) -> None:
        self._sink = sink
        self.states: list[str] = []

    def get_by_name(self, name: str):
        if name == "sink":
            return self._sink
        return None

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def get_state(self, _timeout_ns: int):
        return "success", "playing", "void-pending"


class _FakeGst:
    SECOND = 1_000_000_000
    MapFlags = SimpleNamespace(READ="read")
    State = SimpleNamespace(PLAYING="playing", NULL="null")
    StateChangeReturn = SimpleNamespace(FAILURE="failure", SUCCESS="success")

    def __init__(self, pipeline: _FakePipeline) -> None:
        self.pipeline = pipeline
        self.pipeline_description = ""

    def parse_launch(self, pipeline_description: str) -> _FakePipeline:
        self.pipeline_description = pipeline_description
        return self.pipeline


def _bgr_bytes(width: int, height: int) -> bytes:
    return np.zeros((height, width, 3), dtype=np.uint8).tobytes()


class CameraTests(unittest.TestCase):
    def test_open_camera_builds_awisp_gstreamer_pipeline(self) -> None:
        sink = _FakeSink([None])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)

        with patch.dict(
            os.environ,
            {
                "ATTENDANCE_CAMERA_PIPELINE": "",
                "ATTENDANCE_CAMERA_DEVICE": "/dev/video7",
                "ATTENDANCE_CAMERA_WIDTH": "1280",
                "ATTENDANCE_CAMERA_HEIGHT": "720",
                "ATTENDANCE_CAMERA_FRAMERATE": "45",
            },
            clear=False,
        ):
            with patch("src.camera._load_gst", return_value=gst):
                camera = open_camera()

        self.assertEqual(camera.source_name, "GStreamer pipeline (/dev/video7)")
        self.assertIn("v4l2src device=/dev/video7", gst.pipeline_description)
        self.assertIn("en-awisp=1", gst.pipeline_description)
        self.assertIn("format=I420", gst.pipeline_description)
        self.assertIn("width=1280", gst.pipeline_description)
        self.assertIn("height=720", gst.pipeline_description)
        self.assertIn("max-rate=45", gst.pipeline_description)
        self.assertIn("videoconvert", gst.pipeline_description)
        self.assertIn("format=BGR", gst.pipeline_description)
        self.assertIn("sync=false", gst.pipeline_description)
        self.assertEqual(pipeline.states, ["playing"])

    def test_open_camera_falls_back_to_plain_pipeline_when_awisp_is_unavailable(self) -> None:
        sink = _FakeSink([None])
        plain_pipeline = _FakePipeline(sink)
        gst = _FakeGst(plain_pipeline)
        descriptions: list[str] = []

        def parse_launch(description: str) -> _FakePipeline:
            descriptions.append(description)
            if "en-awisp" in description:
                raise RuntimeError("no property named en-awisp")
            return plain_pipeline

        gst.parse_launch = parse_launch

        with patch.dict(os.environ, {"ATTENDANCE_CAMERA_PIPELINE": ""}, clear=False):
            with patch("src.camera._load_gst", return_value=gst):
                camera = open_camera()

        self.assertEqual(len(descriptions), 2)
        self.assertIn("en-awisp=1", descriptions[0])
        self.assertNotIn("en-awisp", descriptions[1])
        self.assertNotIn("en-awisp", camera.pipeline_description)

    def test_open_camera_uses_configured_pipeline_verbatim(self) -> None:
        sink = _FakeSink([None])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)
        configured_pipeline = (
            "v4l2src device=/dev/video9 "
            "en-awisp=1 en-largemode=0 ! "
            "video/x-raw,format=I420,width=1920,height=1080,framerate=60/1 ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true"
        )

        with patch.dict(os.environ, {"ATTENDANCE_CAMERA_PIPELINE": configured_pipeline}, clear=False):
            with patch("src.camera._load_gst", return_value=gst):
                camera = open_camera()

        self.assertEqual(camera.source_name, "Configured GStreamer pipeline")
        self.assertEqual(gst.pipeline_description, configured_pipeline)

    def test_read_decodes_bgr_frame_and_folds_rotation_into_single_flip(self) -> None:
        width = 8
        height = 4
        buffer = _FakeBuffer(_bgr_bytes(width, height))
        sink = _FakeSink([_FakeSample(buffer)])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)
        flipped = np.full((height, width, 3), 2, dtype=np.uint8)

        with patch("src.camera._load_gst", return_value=gst):
            camera = CameraStream(width=width, height=height, framerate=60)
            camera.start()

        # Default transform is rotate-180 + horizontal flip, which the reader
        # folds into a single vertical flip (rotate180 == flip(-1); flip(-1)
        # then flip(1) == flip(0)).
        with patch("src.camera.cv2.rotate") as rotate:
            with patch("src.camera.cv2.flip", return_value=flipped) as flip:
                ok, frame = camera.read()

        self.assertTrue(ok)
        self.assertIs(frame, flipped)
        self.assertEqual(sink.emit_calls[0][0], "try-pull-sample")
        rotate.assert_not_called()
        flip.assert_called_once()
        self.assertEqual(flip.call_args.args[0].shape, (height, width, 3))
        self.assertEqual(flip.call_args.args[1], 0)
        self.assertTrue(buffer.unmapped)

    def test_read_rejects_frame_with_unexpected_size(self) -> None:
        width = 8
        height = 4
        buffer = _FakeBuffer(b"\x00" * 10)
        sink = _FakeSink([_FakeSample(buffer)])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)

        with patch("src.camera._load_gst", return_value=gst):
            camera = CameraStream(width=width, height=height, framerate=60)
            camera.start()
            with self.assertRaisesRegex(RuntimeError, "unexpected frame size"):
                camera.get_frame()

        self.assertTrue(buffer.unmapped)

    def test_read_returns_false_when_pipeline_yields_no_sample(self) -> None:
        sink = _FakeSink([None])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)

        with patch("src.camera._load_gst", return_value=gst):
            camera = CameraStream()
            camera.start()
            ok, frame = camera.read()

        self.assertFalse(ok)
        self.assertIsNone(frame)

    def test_open_camera_raises_clear_error_when_gstreamer_is_unavailable(self) -> None:
        with patch(
            "src.camera._load_gst",
            side_effect=RuntimeError("The camera backend now uses GStreamer directly."),
        ):
            with self.assertRaisesRegex(RuntimeError, "GStreamer"):
                open_camera()

    def test_release_stops_the_pipeline(self) -> None:
        sink = _FakeSink([None])
        pipeline = _FakePipeline(sink)
        gst = _FakeGst(pipeline)

        with patch("src.camera._load_gst", return_value=gst):
            camera = CameraStream()
            camera.start()
            camera.release()

        self.assertEqual(pipeline.states, ["playing", "null"])


if __name__ == "__main__":
    unittest.main()
