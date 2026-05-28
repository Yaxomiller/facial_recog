import csv
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import ATTENDANCE_DIR, ENCODINGS_FILE, EXPORT_DIR, FACES_DIR, TRAINER_FILE
from src.db import attendance_exists, get_person_by_name, init_database, insert_attendance


def ensure_directories() -> None:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    ATTENDANCE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    init_database()


def person_directory(name: str) -> Path:
    safe_name = name.strip().replace(" ", "_")
    return FACES_DIR / safe_name


def save_encodings(payload: dict[str, list[Any]]) -> None:
    ensure_directories()
    with ENCODINGS_FILE.open("wb") as file:
        pickle.dump(payload, file)


def load_encodings() -> dict[str, list[Any]]:
    if not ENCODINGS_FILE.exists():
        return {"names": [], "labels": {}}

    with ENCODINGS_FILE.open("rb") as file:
        return pickle.load(file)


def trainer_exists() -> bool:
    return TRAINER_FILE.exists()


def trainer_file() -> Path:
    ensure_directories()
    return TRAINER_FILE


def attendance_file_for_today() -> Path:
    ensure_directories()
    return ATTENDANCE_DIR / f"{datetime.now():%Y-%m-%d}.csv"


def attendance_already_marked(name: str) -> bool:
    person = get_person_by_name(name)
    if person is None:
        return False

    return attendance_exists(person["id"], datetime.now().strftime("%Y-%m-%d"))


def mark_attendance(name: str, confidence: float | None = None, source: str = "camera") -> bool:
    person = get_person_by_name(name)
    if person is None:
        return False

    target_file = attendance_file_for_today()
    file_exists = target_file.exists()
    today = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M:%S")

    if attendance_exists(person["id"], today):
        return False

    inserted = insert_attendance(
        person_id=person["id"],
        attendance_date=today,
        attendance_time=current_time,
        status="Present",
        confidence=confidence,
        source=source,
    )
    if not inserted:
        return False

    with target_file.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["name", "date", "time", "status"])

        writer.writerow([name, today, current_time, "Present"])

    return True
