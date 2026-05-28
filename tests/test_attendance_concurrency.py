from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import src.v2.repository as scalable_repo


class AttendanceConcurrencyTests(unittest.TestCase):
    def test_mark_attendance_suppresses_duplicates_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_file = scalable_repo.SCALABLE_DB_FILE
            temp_db_file = Path(temp_dir) / "scalable_attendance.db"
            scalable_repo.SCALABLE_DB_FILE = temp_db_file
            try:
                scalable_repo.init_schema()
                worker = scalable_repo.upsert_worker(employee_code="EMP001", name="Alice")
                worker_id = int(worker["id"])

                barrier = threading.Barrier(4)

                def attempt_mark() -> bool:
                    barrier.wait(timeout=5)
                    return scalable_repo.mark_attendance(
                        worker_id=worker_id,
                        camera_id="gate-1",
                        matched_score=0.93,
                    )

                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _index: attempt_mark(), range(4)))

                self.assertEqual(sum(1 for result in results if result), 1)
                self.assertEqual(sum(1 for result in results if not result), 3)

                with closing(sqlite3.connect(temp_db_file, timeout=30)) as connection:
                    row_count = connection.execute("SELECT COUNT(*) FROM attendance_events").fetchone()[0]
                self.assertEqual(row_count, 1)
            finally:
                scalable_repo.SCALABLE_DB_FILE = original_db_file


if __name__ == "__main__":
    unittest.main()
