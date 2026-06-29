"""Camera recognition loop backed by the v2 ScalableAttendanceService.

Used by the Tkinter GUI so all three app modes (web, native, gui) run
the same recognition pipeline — open-set gate, multi-frame confirmation,
ambiguity rejection, and face quality checks all apply equally.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.camera import open_camera
from src.v2 import repository
from src.v2.service import ScalableAttendanceService


_CAMERA_ID = "gui"
_WINDOW_NAME = "Attendance Recognition (v2)"


def _encode_frame(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("Could not encode frame.")
    return buf.tobytes()


def _draw_result(frame: np.ndarray, result) -> None:
    for box, debug in zip(result.boxes, result.debug_faces):
        x, y, w, h = box.x, box.y, box.width, box.height
        accepted = debug.accepted

        if accepted and result.matches:
            match = next((m for m in result.matches), None)
            label = f"{match.name} ({match.score:.2f})" if match else "Accepted"
            color = (0, 200, 0)
        else:
            reason = debug.reason or "Unknown"
            # Shorten long rejection messages for the overlay
            if reason.startswith("Rejected: "):
                reason = reason[len("Rejected: "):]
            if reason.startswith("Pending: "):
                reason = reason[len("Pending: "):]
            label = reason[:50]
            color = (0, 0, 220) if "Rejected" in (debug.reason or "") else (0, 140, 255)

        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        cv2.rectangle(frame, (x, y + h - 30), (x + w, y + h), color, cv2.FILLED)
        cv2.putText(
            frame,
            label,
            (x + 5, y + h - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    if len(result.boxes) == 0:
        cv2.putText(
            frame,
            "No face detected",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (100, 100, 100),
            1,
            cv2.LINE_AA,
        )


def recognize_and_mark_v2() -> None:
    """Open the camera and run recognition using the v2 service until the window is closed."""
    service = ScalableAttendanceService()
    camera = open_camera()
    cv2.namedWindow(_WINDOW_NAME, cv2.WINDOW_NORMAL)

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError("Could not read frame from camera.")

            try:
                frame_bytes = _encode_frame(frame)
                result = service.recognize(frame_bytes, camera_id=_CAMERA_ID)
            except Exception:
                cv2.imshow(_WINDOW_NAME, frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                if cv2.getWindowProperty(_WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    break
                continue

            for match in result.matches:
                was_marked = repository.mark_attendance(
                    worker_id=match.worker_id,
                    camera_id=_CAMERA_ID,
                    matched_score=match.score,
                )
                if was_marked:
                    print(f"Attendance marked for {match.name} (score={match.score:.3f})")

            _draw_result(frame, result)
            cv2.imshow(_WINDOW_NAME, frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if cv2.getWindowProperty(_WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
