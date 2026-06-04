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
        with (
            patch.object(api_v2.local_camera_proxy, "is_running", return_value=False),
            patch.object(api_v2.local_camera_proxy, "start", return_value="Radxa GStreamer pipeline (/dev/video0)") as start,
            patch.object(
                api_v2.local_camera_proxy,
                "get_frame_bytes",
                side_effect=[b"jpeg-bytes", RuntimeError("stream complete")],
            ),
            patch.object(api_v2.time, "sleep", return_value=None),
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


if __name__ == "__main__":
    unittest.main()
