import cv2
import numpy as np

from src.config import FACE_SIZE
from src.config import FACES_DIR
from src.storage import ensure_directories, person_directory, save_encodings, trainer_file
from src.vision import detect_faces, extract_face_region


def train_model() -> None:
    ensure_directories()
    names: list[str] = []
    faces: list[object] = []
    labels: list[int] = []
    label_to_name: dict[int, str] = {}
    next_label = 0

    for person_dir in sorted(FACES_DIR.iterdir()):
        if not person_dir.is_dir():
            continue

        person_name = person_dir.name.replace("_", " ")
        person_has_samples = False
        for image_path in sorted(person_dir.glob("*.jpg")):
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                print(f"Skipped {image_path}: unreadable image.")
                continue

            detected_faces = detect_faces(image)
            if len(detected_faces) == 0:
                normalized_face = cv2.resize(image, FACE_SIZE)
            else:
                primary_face = max(detected_faces, key=lambda rect: rect[2] * rect[3])
                normalized_face = extract_face_region(image, primary_face)

            names.append(person_name)
            faces.append(normalized_face)
            labels.append(next_label)
            person_has_samples = True

        if person_has_samples:
            label_to_name[next_label] = person_name
            next_label += 1

    if not faces:
        raise RuntimeError("No training faces were created. Add enrollment images and try again.")

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.train(faces, np.array(labels))
    recognizer.save(str(trainer_file()))
    save_encodings({"labels": label_to_name, "names": names})
    print(f"Training complete. Stored {len(faces)} face samples for {len(label_to_name)} people.")
