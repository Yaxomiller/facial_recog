import shutil

from src.config import ENCODINGS_FILE, FACES_DIR, TRAINER_FILE
from src.db import delete_person_by_name, list_people
from src.storage import ensure_directories, person_directory
from src.training import train_model


def delete_person(name: str) -> None:
    ensure_directories()
    face_dir = person_directory(name)

    deleted = delete_person_by_name(name)
    if not deleted:
        raise RuntimeError(f"No saved person found with the name '{name}'.")

    if face_dir.exists():
        shutil.rmtree(face_dir)

    _refresh_model_artifacts()


def _refresh_model_artifacts() -> None:
    remaining_people = list_people()
    if remaining_people:
        train_model()
        return

    if TRAINER_FILE.exists():
        TRAINER_FILE.unlink()

    if ENCODINGS_FILE.exists():
        ENCODINGS_FILE.unlink()
