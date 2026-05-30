from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import cv2
from flask import Flask, Response, jsonify, request


CAMERA_DEVICE = os.getenv("ATTENDANCE_FLASK_CAMERA_DEVICE", "/dev/video0").strip() or "/dev/video0"
FRAME_WIDTH = int(os.getenv("ATTENDANCE_FLASK_CAMERA_WIDTH", "1920"))
FRAME_HEIGHT = int(os.getenv("ATTENDANCE_FLASK_CAMERA_HEIGHT", "1080"))
FRAME_RATE = int(os.getenv("ATTENDANCE_FLASK_CAMERA_FPS", "30"))
JPEG_QUALITY = max(40, min(100, int(os.getenv("ATTENDANCE_FLASK_JPEG_QUALITY", "88"))))
STREAM_FRAME_DELAY_SECONDS = max(0.01, float(os.getenv("ATTENDANCE_FLASK_STREAM_FRAME_DELAY_SECONDS", "0.05")))
APP_HOST = os.getenv("ATTENDANCE_FLASK_HOST", "127.0.0.1").strip() or "127.0.0.1"
APP_PORT = int(os.getenv("ATTENDANCE_FLASK_PORT", "5051"))
CAP_GSTREAMER = getattr(cv2, "CAP_GSTREAMER", 1800)


def build_i420_pipeline(device_path: str) -> str:
    return (
        f"v4l2src device={device_path} en-awisp=1 en-largemode=0 ! "
        f"video/x-raw,format=I420,width={FRAME_WIDTH},height={FRAME_HEIGHT},"
        f"framerate={FRAME_RATE}/1 ! appsink drop=true sync=false"
    )


DEFAULT_PIPELINE = build_i420_pipeline(CAMERA_DEVICE)
CAMERA_PIPELINE = os.getenv("ATTENDANCE_FLASK_CAMERA_PIPELINE", DEFAULT_PIPELINE).strip() or DEFAULT_PIPELINE


@dataclass
class CameraStatus:
    running: bool
    device: str
    pipeline: str
    last_error: str


class LocalCameraBridge:
    def __init__(self, device_path: str, pipeline: str) -> None:
        self.device_path = device_path
        self.pipeline = pipeline
        self._lock = threading.RLock()
        self._capture = None
        self._last_error = ""

    def open(self) -> CameraStatus:
        with self._lock:
            if self._capture is not None and self._capture.isOpened():
                return self.status()

            capture = cv2.VideoCapture(self.pipeline, CAP_GSTREAMER)
            if not capture.isOpened():
                capture.release()
                self._last_error = (
                    f"Could not open {self.device_path} with the configured GStreamer pipeline."
                )
                raise RuntimeError(self._last_error)

            self._capture = capture
            self._last_error = ""
            return self.status()

    def close(self) -> CameraStatus:
        with self._lock:
            if self._capture is not None:
                self._capture.release()
                self._capture = None
            self._last_error = ""
            return self.status()

    def status(self) -> CameraStatus:
        running = self._capture is not None and self._capture.isOpened()
        return CameraStatus(
            running=running,
            device=self.device_path,
            pipeline=self.pipeline,
            last_error=self._last_error,
        )

    def read_bgr_frame(self):
        with self._lock:
            if self._capture is None or not self._capture.isOpened():
                self.open()

            ok, frame = self._capture.read()
            if not ok:
                self._last_error = f"Could not read a frame from {self.device_path}."
                raise RuntimeError(self._last_error)

        try:
            return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
        except cv2.error as exc:
            self._last_error = f"Could not decode an I420 frame from {self.device_path}."
            raise RuntimeError(self._last_error) from exc

    def read_jpeg(self) -> bytes:
        frame = self.read_bgr_frame()
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            self._last_error = "Could not encode the current frame as JPEG."
            raise RuntimeError(self._last_error)
        return encoded.tobytes()

    def mjpeg_stream(self):
        while True:
            frame_bytes = self.read_jpeg()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-store\r\n\r\n" + frame_bytes + b"\r\n"
            )
            time.sleep(STREAM_FRAME_DELAY_SECONDS)


app = Flask(__name__)
camera_bridge = LocalCameraBridge(device_path=CAMERA_DEVICE, pipeline=CAMERA_PIPELINE)


@app.after_request
def add_cors_headers(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def status_payload(status: CameraStatus) -> dict[str, object]:
    return {
        "running": status.running,
        "device": status.device,
        "pipeline": status.pipeline,
        "last_error": status.last_error,
        "frame_url": "/camera/frame.jpg",
        "stream_url": "/camera/stream.mjpg",
    }


@app.route("/", methods=["GET"])
def index() -> str:
    return f"""
    <html>
      <head>
        <title>Flask Camera Bridge</title>
        <style>
          body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #f6f7fb; color: #14213d; }}
          .card {{ background: white; border-radius: 16px; padding: 20px; max-width: 860px; box-shadow: 0 10px 30px rgba(20,33,61,0.08); }}
          .row {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
          button {{ padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; }}
          .primary {{ background: #17305f; color: white; }}
          .ghost {{ background: #e9eef7; color: #17305f; }}
          pre {{ background: #f1f4fa; padding: 14px; border-radius: 12px; overflow: auto; }}
          img {{ max-width: 100%; border-radius: 12px; background: #111827; }}
        </style>
      </head>
      <body>
        <div class="card">
          <h1>Flask Camera Bridge</h1>
          <p>Device: <code>{CAMERA_DEVICE}</code></p>
          <p>Pipeline: <code>{CAMERA_PIPELINE}</code></p>
          <div class="row">
            <button class="primary" onclick="openCamera()">Open Camera</button>
            <button class="ghost" onclick="closeCamera()">Close Camera</button>
            <button class="ghost" onclick="refreshStatus()">Refresh Status</button>
          </div>
          <pre id="status">Loading...</pre>
          <img id="preview" src="/camera/frame.jpg?ts=0" alt="Camera Preview" />
        </div>
        <script>
          async function refreshStatus() {{
            const response = await fetch('/camera/status');
            const payload = await response.json();
            document.getElementById('status').textContent = JSON.stringify(payload, null, 2);
            document.getElementById('preview').src = '/camera/frame.jpg?ts=' + Date.now();
          }}
          async function openCamera() {{
            const response = await fetch('/camera/open', {{ method: 'POST' }});
            const payload = await response.json();
            document.getElementById('status').textContent = JSON.stringify(payload, null, 2);
            document.getElementById('preview').src = '/camera/frame.jpg?ts=' + Date.now();
          }}
          async function closeCamera() {{
            const response = await fetch('/camera/close', {{ method: 'POST' }});
            const payload = await response.json();
            document.getElementById('status').textContent = JSON.stringify(payload, null, 2);
          }}
          refreshStatus();
        </script>
      </body>
    </html>
    """


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/camera/status", methods=["GET"])
def camera_status():
    return jsonify(status_payload(camera_bridge.status()))


@app.route("/camera/open", methods=["POST", "OPTIONS"])
def camera_open():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        status = camera_bridge.open()
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), **status_payload(camera_bridge.status())}), 500
    return jsonify({"ok": True, **status_payload(status)})


@app.route("/camera/close", methods=["POST", "OPTIONS"])
def camera_close():
    if request.method == "OPTIONS":
        return ("", 204)
    status = camera_bridge.close()
    return jsonify({"ok": True, **status_payload(status)})


@app.route("/camera/frame.jpg", methods=["GET"])
def camera_frame():
    try:
        frame_bytes = camera_bridge.read_jpeg()
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), **status_payload(camera_bridge.status())}), 500
    return Response(frame_bytes, mimetype="image/jpeg")


@app.route("/camera/stream.mjpg", methods=["GET"])
def camera_stream():
    try:
        camera_bridge.open()
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc), **status_payload(camera_bridge.status())}), 500
    return Response(
        camera_bridge.mjpeg_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
