import cv2
from flask import Flask, Response
import sys

app = Flask(_name_)

# The proven, raw I420 GStreamer pipeline for Allwinner A527
GST_PIPELINE = (
    "v4l2src device=/dev/video0 en-awisp=1 en-largemode=0 ! "
    "video/x-raw,format=I420,width=1920,height=1080,framerate=30/1 ! "
    "appsink drop=true sync=false"
)

# Initialize the camera globally so it doesn't rapidly open/close on page refreshes
print("Initializing camera pipeline...")
cap = cv2.VideoCapture(GST_PIPELINE, cv2.CAP_GSTREAMER)

if not cap.isOpened():
    print("\n[CRITICAL ERROR] The camera failed to open.")
    print("This almost always means another process is locking /dev/video0.")
    print("Run this command in your terminal, then try again:")
    print("sudo fuser -k /dev/video0\n")
    sys.exit(1)

def generate_frames():
    """Reads frames from the camera, converts color, and encodes to JPEG."""
    while True:
        success, frame = cap.read()
        if not success:
            print("Warning: Dropped a frame from the camera.")
            continue

        # Decode the raw Allwinner I420 color format into standard BGR
        try:
            color_frame = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
        except cv2.error:
            continue

        # Compress the frame to JPEG
        ret, buffer = cv2.imencode('.jpg', color_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ret:
            continue

        frame_bytes = buffer.tobytes()

        # Yield the continuous byte stream (MJPEG format)
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


@app.route('/stream.mjpg')
def video_feed():
    """The actual endpoint that React will connect to."""
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/')
def index():
    """A simple built-in webpage to test if the stream is working."""
    return """
    <html>
      <body style="background-color: #1a1a1a; text-align: center; color: white; font-family: sans-serif;">
        <h2>Radxa 13M Minimal Stream</h2>
        <img src='/stream.mjpg' style="max-width: 80%; border: 3px solid #444; border-radius: 8px;" />
      </body>
    </html>
    """


if _name_ == '_main_':
    print("\n==============================================")
    print(" CAMERA ACTIVE. SERVER STARTING ON PORT 5051")
    print("==============================================\n")
    # Run the server. Threaded=True prevents browser lockups.
    app.run(host='0.0.0.0', port=5051, debug=False, threaded=True)