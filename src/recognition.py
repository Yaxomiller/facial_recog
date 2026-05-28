import cv2

from src.config import (
    CAMERA_INDEX,
    LBPH_CONFIDENCE_THRESHOLD,
    LIVENESS_EYE_CLOSED_FRAMES,
    LIVENESS_EYE_OPEN_FRAMES,
)
from src.storage import load_encodings, mark_attendance, trainer_exists, trainer_file
from src.vision import detect_eyes, detect_faces, extract_face_region


class BlinkLivenessTracker:
    def __init__(self, min_open_frames: int, min_closed_frames: int) -> None:
        self.min_open_frames = min_open_frames
        self.min_closed_frames = min_closed_frames
        self.active_name: str | None = None
        self.open_frames = 0
        self.closed_frames = 0
        self.blink_started = False

    def update(self, name: str, eyes_detected: int) -> tuple[bool, str]:
        if self.active_name != name:
            self.active_name = name
            self.open_frames = 0
            self.closed_frames = 0
            self.blink_started = False

        if eyes_detected > 0:
            self.open_frames += 1
            self.closed_frames = 0
        else:
            self.closed_frames += 1
            self.open_frames = 0

        if not self.blink_started:
            if self.closed_frames >= self.min_closed_frames:
                self.blink_started = True
                return False, "Open your eyes"
            return False, "Blink your eyes"

        if self.open_frames >= self.min_open_frames:
            self._reset()
            return True, "Liveness passed"

        return False, "Open your eyes"

    def _reset(self) -> None:
        self.active_name = None
        self.open_frames = 0
        self.closed_frames = 0
        self.blink_started = False


def recognize_and_mark() -> None:
    window_name = "Attendance Recognition"
    known_data = load_encodings()
    label_map = known_data.get("labels", {})

    if not trainer_exists() or not label_map:
        raise RuntimeError("No trained model found. Run the train command first.")

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(str(trainer_file()))

    camera = cv2.VideoCapture(CAMERA_INDEX)
    if not camera.isOpened():
        raise RuntimeError("Could not open webcam.")

    liveness_tracker = BlinkLivenessTracker(
        min_open_frames=LIVENESS_EYE_OPEN_FRAMES,
        min_closed_frames=LIVENESS_EYE_CLOSED_FRAMES,
    )
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError("Could not read frame from webcam.")

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            face_locations = detect_faces(gray)

            for (x, y, w, h) in face_locations:
                name = "Unknown"
                confidence = None
                label = "Unknown"
                instruction = "Align face with camera"
                normalized_face = extract_face_region(gray, (x, y, w, h))
                predicted_label, raw_confidence = recognizer.predict(normalized_face)

                if raw_confidence <= LBPH_CONFIDENCE_THRESHOLD and predicted_label in label_map:
                    name = label_map[predicted_label]
                    confidence = float(max(0.0, min(1.0, 1 - (raw_confidence / 100.0))))
                    face_roi = gray[y : y + h, x : x + w]
                    eyes_detected = len(detect_eyes(face_roi))
                    live_face, instruction = liveness_tracker.update(name, eyes_detected)
                    if live_face:
                        was_marked = mark_attendance(name, confidence=confidence, source="camera+liveness")
                        if was_marked:
                            print(f"Attendance marked for {name}")
                        label = f"{name} ({confidence:.2f})"
                    else:
                        label = f"{name} - {instruction}"

                color = (0, 200, 0) if name != "Unknown" else (0, 0, 255)
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                cv2.rectangle(frame, (x, y + h - 35), (x + w, y + h), color, cv2.FILLED)
                cv2.putText(frame, label, (x + 6, y + h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            cv2.imshow(window_name, frame)
            key = cv2.waitKey(1) & 0xFF
            window_visible = cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) >= 1
            if key in (ord("q"), 27) or not window_visible:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
