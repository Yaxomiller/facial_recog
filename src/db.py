import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from src.config import DATABASE_FILE


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database() -> None:
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                face_folder TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                attendance_date TEXT NOT NULL,
                attendance_time TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(person_id, attendance_date),
                FOREIGN KEY (person_id) REFERENCES people(id)
            )
            """
        )


def upsert_person(name: str, face_folder: Path) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO people (name, face_folder, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET face_folder = excluded.face_folder
            """,
            (name, str(face_folder), now),
        )


def get_person_by_name(name: str) -> sqlite3.Row | None:
    with get_connection() as connection:
        cursor = connection.execute("SELECT * FROM people WHERE name = ?", (name,))
        return cursor.fetchone()


def list_people() -> list[sqlite3.Row]:
    with get_connection() as connection:
        cursor = connection.execute("SELECT * FROM people ORDER BY name ASC")
        return cursor.fetchall()


def delete_person_by_name(name: str) -> bool:
    with get_connection() as connection:
        person = connection.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        if person is None:
            return False

        connection.execute("DELETE FROM attendance WHERE person_id = ?", (person["id"],))
        cursor = connection.execute("DELETE FROM people WHERE name = ?", (name,))
        return cursor.rowcount > 0


def attendance_exists(person_id: int, attendance_date: str) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            "SELECT 1 FROM attendance WHERE person_id = ? AND attendance_date = ?",
            (person_id, attendance_date),
        )
        return cursor.fetchone() is not None


def insert_attendance(
    person_id: int,
    attendance_date: str,
    attendance_time: str,
    status: str,
    confidence: float | None,
    source: str,
) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO attendance (
                person_id, attendance_date, attendance_time, status, confidence, source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (person_id, attendance_date, attendance_time, status, confidence, source, now),
        )
        return cursor.rowcount > 0


def list_attendance(limit: int = 100) -> list[sqlite3.Row]:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT attendance.id, people.name, attendance.attendance_date, attendance.attendance_time,
                   attendance.status, attendance.confidence, attendance.source
            FROM attendance
            JOIN people ON people.id = attendance.person_id
            ORDER BY attendance.attendance_date DESC, attendance.attendance_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()


def attendance_between(start_date: str, end_date: str) -> list[sqlite3.Row]:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT people.name, attendance.attendance_date, attendance.attendance_time,
                   attendance.status, attendance.confidence, attendance.source
            FROM attendance
            JOIN people ON people.id = attendance.person_id
            WHERE attendance.attendance_date BETWEEN ? AND ?
            ORDER BY attendance.attendance_date ASC, attendance.attendance_time ASC
            """,
            (start_date, end_date),
        )
        return cursor.fetchall()
