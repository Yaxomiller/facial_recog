import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import numpy as np

from src.v2.config import ATTENDANCE_COOLDOWN_HOURS, SCALABLE_DB_FILE


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(SCALABLE_DB_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_schema() -> None:
    SCALABLE_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                backend TEXT NOT NULL DEFAULT 'histogram',
                dimension INTEGER NOT NULL DEFAULT 0,
                face_image BLOB,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                camera_id TEXT NOT NULL,
                matched_score REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            )
            """
        )
        _ensure_embedding_columns(connection)
        _ensure_attendance_columns(connection)


def _ensure_embedding_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(worker_embeddings)").fetchall()
    }
    if "backend" not in existing_columns:
        connection.execute("ALTER TABLE worker_embeddings ADD COLUMN backend TEXT NOT NULL DEFAULT 'histogram'")
    if "dimension" not in existing_columns:
        connection.execute("ALTER TABLE worker_embeddings ADD COLUMN dimension INTEGER NOT NULL DEFAULT 0")
    if "face_image" not in existing_columns:
        connection.execute("ALTER TABLE worker_embeddings ADD COLUMN face_image BLOB")


def _ensure_attendance_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(attendance_events)").fetchall()
    }
    if "raw_sensor_value" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN raw_sensor_value REAL")
    if "alcohol_ppb" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN alcohol_ppb REAL NOT NULL DEFAULT 0")
    if "cannabis_ppb" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN cannabis_ppb REAL NOT NULL DEFAULT 0")
    if "alcohol_clear" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN alcohol_clear INTEGER NOT NULL DEFAULT 1")
    if "cannabis_clear" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN cannabis_clear INTEGER NOT NULL DEFAULT 1")
    if "attendance_marked" not in existing_columns:
        connection.execute("ALTER TABLE attendance_events ADD COLUMN attendance_marked INTEGER NOT NULL DEFAULT 1")


def upsert_worker(employee_code: str, name: str) -> sqlite3.Row:
    created_at = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO workers (employee_code, name, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(employee_code) DO UPDATE SET name = excluded.name
            """,
            (employee_code, name, created_at),
        )
        row = connection.execute("SELECT * FROM workers WHERE employee_code = ?", (employee_code,)).fetchone()
        if row is None:
            raise RuntimeError("Worker upsert failed.")
        return row


def list_workers() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workers ORDER BY name ASC").fetchall()


def fetch_worker_by_employee_code(employee_code: str) -> Optional[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workers WHERE employee_code = ?", (employee_code,)).fetchone()


def delete_worker_by_employee_code(employee_code: str) -> Optional[sqlite3.Row]:
    with get_connection() as connection:
        worker = connection.execute("SELECT * FROM workers WHERE employee_code = ?", (employee_code,)).fetchone()
        if worker is None:
            return None

        connection.execute("DELETE FROM attendance_events WHERE worker_id = ?", (worker["id"],))
        connection.execute("DELETE FROM worker_embeddings WHERE worker_id = ?", (worker["id"],))
        connection.execute("DELETE FROM workers WHERE id = ?", (worker["id"],))
        return worker


def store_embedding(worker_id: int, vector: np.ndarray, backend: str, face_image: Optional[bytes] = None) -> None:
    created_at = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO worker_embeddings (worker_id, backend, dimension, face_image, vector, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                backend,
                int(vector.shape[0]),
                face_image,
                vector.astype(np.float32).tobytes(),
                created_at,
            ),
        )


def delete_embeddings_for_worker(worker_id: int, backend: Optional[str] = None) -> None:
    with get_connection() as connection:
        if backend is None:
            connection.execute("DELETE FROM worker_embeddings WHERE worker_id = ?", (worker_id,))
        else:
            connection.execute(
                "DELETE FROM worker_embeddings WHERE worker_id = ? AND backend = ?",
                (worker_id, backend),
            )


def fetch_embeddings(backend: Optional[str] = None, dimension: Optional[int] = None) -> list[tuple[int, np.ndarray]]:
    with get_connection() as connection:
        if backend is None and dimension is None:
            rows = connection.execute(
                "SELECT worker_id, vector FROM worker_embeddings ORDER BY id ASC"
            ).fetchall()
        elif backend is None:
            rows = connection.execute(
                """
                SELECT worker_id, vector
                FROM worker_embeddings
                WHERE dimension = ?
                ORDER BY id ASC
                """,
                (dimension,),
            ).fetchall()
        elif dimension is None:
            rows = connection.execute(
                """
                SELECT worker_id, vector
                FROM worker_embeddings
                WHERE backend = ?
                ORDER BY id ASC
                """,
                (backend,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT worker_id, vector
                FROM worker_embeddings
                WHERE backend = ? AND dimension = ?
                ORDER BY id ASC
                """,
                (backend, dimension),
            ).fetchall()
        return [(row["worker_id"], np.frombuffer(row["vector"], dtype=np.float32)) for row in rows]


def fetch_worker(worker_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()


def fetch_face_samples(backend: str) -> list[tuple[int, bytes]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT worker_id, face_image
            FROM worker_embeddings
            WHERE backend = ? AND face_image IS NOT NULL
            ORDER BY id ASC
            """,
            (backend,),
        ).fetchall()
        return [(int(row["worker_id"]), bytes(row["face_image"])) for row in rows]


def fetch_training_samples(
    backend: str,
    dimension: Optional[int] = None,
) -> list[tuple[int, np.ndarray, Optional[bytes]]]:
    with get_connection() as connection:
        if dimension is None:
            rows = connection.execute(
                """
                SELECT worker_id, vector, face_image
                FROM worker_embeddings
                WHERE backend = ?
                ORDER BY id ASC
                """,
                (backend,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT worker_id, vector, face_image
                FROM worker_embeddings
                WHERE backend = ? AND dimension = ?
                ORDER BY id ASC
                """,
                (backend, dimension),
            ).fetchall()

        return [
            (
                int(row["worker_id"]),
                np.frombuffer(row["vector"], dtype=np.float32),
                None if row["face_image"] is None else bytes(row["face_image"]),
            )
            for row in rows
        ]


def worker_count() -> int:
    with get_connection() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM workers").fetchone()
        return 0 if row is None else int(row["count"])


def embedding_count(backend: Optional[str] = None, dimension: Optional[int] = None) -> int:
    with get_connection() as connection:
        if backend is None and dimension is None:
            row = connection.execute("SELECT COUNT(*) AS count FROM worker_embeddings").fetchone()
        elif backend is None:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE dimension = ?",
                (dimension,),
            ).fetchone()
        elif dimension is None:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE backend = ?",
                (backend,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE backend = ? AND dimension = ?",
                (backend, dimension),
            ).fetchone()
        return 0 if row is None else int(row["count"])


def mark_attendance(worker_id: int, camera_id: str, matched_score: float) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=ATTENDANCE_COOLDOWN_HOURS)).isoformat(timespec="seconds")
    created_at = now.isoformat(timespec="seconds")

    with get_connection() as connection:
        # Serialize the "check recent event -> insert new event" flow so
        # simultaneous scans for the same worker cannot both commit.
        connection.execute("BEGIN IMMEDIATE")
        recent = connection.execute(
            """
            SELECT id FROM attendance_events
            WHERE worker_id = ? AND attendance_marked = 1 AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (worker_id, cutoff),
        ).fetchone()
        if recent is not None:
            return False

        connection.execute(
            "INSERT INTO attendance_events (worker_id, camera_id, matched_score, created_at) VALUES (?, ?, ?, ?)",
            (worker_id, camera_id, matched_score, created_at),
        )
        return True


def record_screening_event(
    worker_id: int,
    camera_id: str,
    matched_score: float,
    alcohol_ppb: float,
    cannabis_ppb: float,
    alcohol_clear: bool,
    cannabis_clear: bool,
    raw_sensor_value: Optional[float] = None,
) -> sqlite3.Row:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=ATTENDANCE_COOLDOWN_HOURS)).isoformat(timespec="seconds")
    created_at = now.isoformat(timespec="seconds")
    overall_clear = alcohol_clear and cannabis_clear
    attendance_marked = False

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if overall_clear:
            recent = connection.execute(
                """
                SELECT id FROM attendance_events
                WHERE worker_id = ? AND attendance_marked = 1 AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (worker_id, cutoff),
            ).fetchone()
            attendance_marked = recent is None

        cursor = connection.execute(
            """
            INSERT INTO attendance_events (
                worker_id,
                camera_id,
                matched_score,
                raw_sensor_value,
                alcohol_ppb,
                cannabis_ppb,
                alcohol_clear,
                cannabis_clear,
                attendance_marked,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                camera_id,
                matched_score,
                None if raw_sensor_value is None else float(raw_sensor_value),
                float(alcohol_ppb),
                float(cannabis_ppb),
                int(alcohol_clear),
                int(cannabis_clear),
                int(attendance_marked),
                created_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM attendance_events WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Could not store the attendance screening event.")
        return row


def list_attendance(limit: int = 100) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT attendance_events.id, attendance_events.worker_id, workers.employee_code, workers.name,
                   attendance_events.camera_id, attendance_events.matched_score,
                   attendance_events.raw_sensor_value,
                   attendance_events.alcohol_ppb, attendance_events.cannabis_ppb,
                   attendance_events.alcohol_clear, attendance_events.cannabis_clear,
                   attendance_events.attendance_marked, attendance_events.created_at
            FROM attendance_events
            JOIN workers ON workers.id = attendance_events.worker_id
            ORDER BY attendance_events.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def delete_attendance_event(attendance_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as connection:
        attendance = connection.execute(
            """
            SELECT attendance_events.id, attendance_events.worker_id, workers.employee_code, workers.name
            FROM attendance_events
            JOIN workers ON workers.id = attendance_events.worker_id
            WHERE attendance_events.id = ?
            """,
            (attendance_id,),
        ).fetchone()
        if attendance is None:
            return None

        connection.execute("DELETE FROM attendance_events WHERE id = ?", (attendance_id,))
        return attendance


def system_counts(backend: Optional[str] = None, dimension: Optional[int] = None) -> dict[str, int]:
    with get_connection() as connection:
        workers = connection.execute("SELECT COUNT(*) AS count FROM workers").fetchone()
        if backend is None and dimension is None:
            embeddings = connection.execute("SELECT COUNT(*) AS count FROM worker_embeddings").fetchone()
        elif backend is None:
            embeddings = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE dimension = ?",
                (dimension,),
            ).fetchone()
        elif dimension is None:
            embeddings = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE backend = ?",
                (backend,),
            ).fetchone()
        else:
            embeddings = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_embeddings WHERE backend = ? AND dimension = ?",
                (backend, dimension),
            ).fetchone()
        attendance = connection.execute("SELECT COUNT(*) AS count FROM attendance_events").fetchone()
        return {
            "workers": 0 if workers is None else int(workers["count"]),
            "embeddings": 0 if embeddings is None else int(embeddings["count"]),
            "attendance_events": 0 if attendance is None else int(attendance["count"]),
        }
