from pathlib import Path

import cv2

from src.config import CAMERA_INDEX, FACES_DIR
from src.db import upsert_person
from src.storage import ensure_directories, person_directory
from src.vision import detect_faces, extract_face_region


def enroll_person(name: str, max_images: int) -> None:
    ensure_directories()
    person_dir = person_directory(name)
    person_dir.mkdir(parents=True, exist_ok=True)

    camera = cv2.VideoCapture(CAMERA_INDEX)
    if not camera.isOpened():
        raise RuntimeError("Could not open webcam.")

    saved_images = len(list(person_dir.glob("*.jpg")))

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError("Could not read frame from webcam.")

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detect_faces(gray)
            for (x, y, w, h) in faces:
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 0), 2)

            prompt = f"Enroll: {name} | Saved: {saved_images}/{max_images} | Press 's' to save, 'q' to quit"
            cv2.putText(frame, prompt, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.imshow("Enrollment", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("s") and saved_images < max_images:
                if len(faces) == 0:
                    print("No face detected. Try again.")
                    continue

                primary_face = max(faces, key=lambda rect: rect[2] * rect[3])
                normalized_face = extract_face_region(gray, primary_face)
                image_path = person_dir / f"{saved_images + 1:02d}.jpg"
                cv2.imwrite(str(image_path), normalized_face)
                saved_images += 1
                print(f"Saved {image_path}")
            elif key == ord("q") or saved_images >= max_images:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()

    if saved_images > 0:
        upsert_person(name=name, face_folder=person_dir)


def list_registered_people() -> list[Path]:
    ensure_directories()
    return [path for path in FACES_DIR.iterdir() if path.is_dir()]
