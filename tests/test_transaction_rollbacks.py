from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import src.db as attendance_db
import src.v2.repository as scalable_repo


class TransactionRollbackTests(unittest.TestCase):
    def test_legacy_db_rolls_back_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_file = attendance_db.DATABASE_FILE
            temp_db_file = Path(temp_dir) / "attendance.db"
            attendance_db.DATABASE_FILE = temp_db_file
            try:
                attendance_db.init_database()

                with self.assertRaises(RuntimeError):
                    with attendance_db.get_connection() as connection:
                        connection.execute(
                            """
                            INSERT INTO people (name, face_folder, created_at)
                            VALUES (?, ?, ?)
                            """,
                            ("alice", "faces/alice", "2026-04-01T18:00:00"),
                        )
                        raise RuntimeError("force rollback")

                with closing(sqlite3.connect(temp_db_file)) as connection:
                    count = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                attendance_db.DATABASE_FILE = original_db_file

    def test_scalable_db_rolls_back_partial_write_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_file = scalable_repo.SCALABLE_DB_FILE
            temp_db_file = Path(temp_dir) / "scalable_attendance.db"
            scalable_repo.SCALABLE_DB_FILE = temp_db_file
            try:
                scalable_repo.init_schema()

                with self.assertRaises(RuntimeError):
                    with scalable_repo.get_connection() as connection:
                        worker_id = connection.execute(
                            """
                            INSERT INTO workers (employee_code, name, created_at)
                            VALUES (?, ?, ?)
                            """,
                            ("EMP001", "Alice", "2026-04-01T18:00:00+00:00"),
                        ).lastrowid
                        connection.execute(
                            """
                            INSERT INTO worker_embeddings (worker_id, backend, dimension, vector, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (worker_id, "classical", 3, b"\x00\x00\x00", "2026-04-01T18:00:01+00:00"),
                        )
                        raise RuntimeError("force rollback")

                with closing(sqlite3.connect(temp_db_file)) as connection:
                    worker_count = connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
                    embedding_count = connection.execute("SELECT COUNT(*) FROM worker_embeddings").fetchone()[0]
                self.assertEqual(worker_count, 0)
                self.assertEqual(embedding_count, 0)
            finally:
                scalable_repo.SCALABLE_DB_FILE = original_db_file


if __name__ == "__main__":
    unittest.main()
