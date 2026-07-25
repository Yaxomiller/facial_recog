from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import src.api_v2 as api_v2


async def _collect_chunks(streaming_response) -> list[bytes]:
    chunks: list[bytes] = []
    async for chunk in streaming_response.body_iterator:
        chunks.append(chunk)
    return chunks


class LocalCameraApiTests(unittest.TestCase):
    def test_local_camera_stream_serves_mjpeg_chunks(self) -> None:
        with patch.object(api_v2.local_camera_proxy, "is_running", return_value=False):
            with patch.object(api_v2.local_camera_proxy, "start", return_value="Radxa GStreamer pipeline (/dev/video0)") as start:
                with patch.object(
                    api_v2.local_camera_proxy,
                    "get_frame",
                    side_effect=[(b"jpeg-bytes", 1), RuntimeError("stream complete")],
                ):
                    response = api_v2.local_camera_stream(None)
                    chunks = asyncio.run(_collect_chunks(response))

        body = b"".join(chunks)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "multipart/x-mixed-replace; boundary=frame")
        self.assertEqual(response.headers["Cache-Control"], "no-store, no-cache, must-revalidate")
        self.assertIn(b"--frame", body)
        self.assertIn(b"jpeg-bytes", body)
        start.assert_called_once_with()

    def test_stream_only_requests_frames_newer_than_the_last_one_sent(self) -> None:
        # Each iteration must ask for a frame strictly newer than the one it
        # just sent, so a slow camera never causes duplicate frames to be
        # pushed to the browser.
        requested: list[object] = []

        def fake_get_frame(timeout_seconds: float = 2.0, after_sequence=None):
            requested.append(after_sequence)
            if len(requested) > 3:
                raise RuntimeError("stream complete")
            return b"frame-%d" % len(requested), len(requested)

        with patch.object(api_v2.local_camera_proxy, "is_running", return_value=True):
            with patch.object(api_v2.local_camera_proxy, "get_frame", side_effect=fake_get_frame):
                response = api_v2.local_camera_stream(None)
                body = b"".join(asyncio.run(_collect_chunks(response)))

        self.assertEqual(requested, [None, 1, 2, 3])
        for index in (1, 2, 3):
            self.assertIn(b"frame-%d" % index, body)


if __name__ == "__main__":
    unittest.main()
