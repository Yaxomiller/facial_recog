from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import src.v2.repository as scalable_repo


class BreathScreeningRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_file = scalable_repo.SCALABLE_DB_FILE
        scalable_repo.SCALABLE_DB_FILE = Path(self.temp_dir.name) / "scalable_attendance.db"
        scalable_repo.init_schema()
        worker = scalable_repo.upsert_worker(employee_code="EMP001", name="Alice")
        self.worker_id = int(worker["id"])

    def tearDown(self) -> None:
        scalable_repo.SCALABLE_DB_FILE = self.original_db_file
        self.temp_dir.cleanup()

    def test_failed_screening_is_recorded_without_marking_attendance(self) -> None:
        event = scalable_repo.record_screening_event(
            worker_id=self.worker_id,
            camera_id="gate-1",
            matched_score=0.91,
            alcohol_ppb=58.0,
            cannabis_ppb=0.0,
            alcohol_clear=False,
            cannabis_clear=True,
        )

        self.assertEqual(float(event["alcohol_ppb"]), 58.0)
        self.assertEqual(float(event["cannabis_ppb"]), 0.0)
        self.assertEqual(int(event["attendance_marked"]), 0)

        history = scalable_repo.list_attendance(limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(int(history[0]["alcohol_clear"]), 0)
        self.assertEqual(int(history[0]["cannabis_clear"]), 1)
        self.assertEqual(int(history[0]["attendance_marked"]), 0)

    def test_second_passing_screening_within_cooldown_is_not_marked(self) -> None:
        first = scalable_repo.record_screening_event(
            worker_id=self.worker_id,
            camera_id="gate-1",
            matched_score=0.95,
            alcohol_ppb=0.0,
            cannabis_ppb=0.0,
            alcohol_clear=True,
            cannabis_clear=True,
        )
        second = scalable_repo.record_screening_event(
            worker_id=self.worker_id,
            camera_id="gate-1",
            matched_score=0.95,
            alcohol_ppb=0.0,
            cannabis_ppb=0.0,
            alcohol_clear=True,
            cannabis_clear=True,
        )

        self.assertEqual(int(first["attendance_marked"]), 1)
        self.assertEqual(int(second["attendance_marked"]), 0)

        history = scalable_repo.list_attendance(limit=10)
        self.assertEqual(len(history), 2)
        self.assertEqual(sum(1 for row in history if int(row["attendance_marked"]) == 1), 1)


if __name__ == "__main__":
    unittest.main()
