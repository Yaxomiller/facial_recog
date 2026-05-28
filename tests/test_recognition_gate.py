from __future__ import annotations

import unittest

import numpy as np

from src.v2.service import ScalableAttendanceService, WorkerProfile


class RecognitionGateTests(unittest.TestCase):
    def _service_with_profile(self, centroid: np.ndarray, sample_count: int = 3, mean_similarity: float = 0.9) -> ScalableAttendanceService:
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {
            1: WorkerProfile(
                centroid=centroid / np.linalg.norm(centroid),
                min_similarity=mean_similarity - 0.04,
                mean_similarity=mean_similarity,
                sample_count=sample_count,
            )
        }
        return service

    def test_rejects_face_without_enough_support_hits(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.84,
            support_scores=[0.84, 0.61],
            query_embedding=np.asarray([0.98, 0.02, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("enrolled samples", reason)

    def test_rejects_face_far_from_worker_centroid(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.87,
            support_scores=[0.87, 0.86, 0.82],
            query_embedding=np.asarray([0.2, 0.98, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("enrolled profile", reason)

    def test_rejects_profile_with_too_few_enrollment_images(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid, sample_count=1, mean_similarity=1.0)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.8,
            support_scores=[0.8],
            query_embedding=np.asarray([0.8, 0.6, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("Re-enroll", reason)

    def test_single_profile_database_rejects_score_below_realistic_floor(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid, sample_count=3, mean_similarity=0.9)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.73,
            support_scores=[0.73, 0.72, 0.71],
            query_embedding=np.asarray([0.995, 0.05, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("verification threshold", reason)

    def test_single_profile_allows_reasonable_centroid_match_when_scores_are_strong(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid, sample_count=3, mean_similarity=0.97)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.88,
            support_scores=[0.88, 0.85, 0.83],
            query_embedding=np.asarray([0.734, 0.679, 0.0], dtype=np.float32),
        )

        self.assertTrue(accepted)
        self.assertEqual(reason, "")

    def test_single_profile_calibrates_real_match_into_confirmation_band(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid, sample_count=3, mean_similarity=0.92)

        confidence = service._calibrated_match_confidence(
            worker_id=1,
            raw_score=0.773,
            support_scores=[0.773, 0.768, 0.761],
            query_embedding=np.asarray([0.93, 0.367, 0.0], dtype=np.float32),
        )

        self.assertGreaterEqual(confidence, 0.86)
        self.assertLessEqual(confidence, 0.99)

    def test_multi_profile_centroid_threshold_is_capped_at_point_seven(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {
            1: WorkerProfile(
                centroid=centroid / np.linalg.norm(centroid),
                min_similarity=0.94,
                mean_similarity=0.977,
                sample_count=3,
            ),
            2: WorkerProfile(
                centroid=np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
                min_similarity=0.90,
                mean_similarity=0.95,
                sample_count=3,
            ),
        }

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.88,
            support_scores=[0.88, 0.84, 0.82],
            query_embedding=np.asarray([0.734, 0.679, 0.0], dtype=np.float32),
        )

        self.assertTrue(accepted)
        self.assertEqual(reason, "")

    def test_accepts_face_with_strong_centroid_and_support(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = self._service_with_profile(centroid)

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.86,
            support_scores=[0.86, 0.79, 0.73],
            query_embedding=np.asarray([0.99, 0.04, 0.0], dtype=np.float32),
        )

        self.assertTrue(accepted)
        self.assertEqual(reason, "")

    def test_rejects_score_that_is_far_below_a_strong_profile_baseline(self) -> None:
        centroid = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {
            1: WorkerProfile(
                centroid=centroid / np.linalg.norm(centroid),
                min_similarity=0.92,
                mean_similarity=0.96,
                sample_count=4,
            ),
            2: WorkerProfile(
                centroid=np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
                min_similarity=0.90,
                mean_similarity=0.94,
                sample_count=4,
            ),
        }

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.74,
            support_scores=[0.74, 0.73, 0.72],
            query_embedding=np.asarray([0.98, 0.04, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("verification threshold", reason)

    def test_rejects_when_descriptor_profile_is_missing(self) -> None:
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {}

        accepted, reason = service._passes_open_set_gate(
            worker_id=1,
            best_score=0.91,
            support_scores=[0.91, 0.88, 0.84],
            query_embedding=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        )

        self.assertFalse(accepted)
        self.assertIn("Re-enroll", reason)

    def test_strict_confirmation_mode_requires_more_consistent_frames(self) -> None:
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {
            1: WorkerProfile(
                centroid=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
                min_similarity=0.9,
                mean_similarity=0.94,
                sample_count=3,
            )
        }
        service.pending_matches = {}

        confirmed, score, reason = service._confirm_match_candidate(
            camera_id="cam-1",
            worker_id=1,
            score=0.89,
            strict_mode=True,
        )
        self.assertFalse(confirmed)
        self.assertIsNone(reason)

        for _index in range(3):
            confirmed, score, reason = service._confirm_match_candidate(
                camera_id="cam-1",
                worker_id=1,
                score=0.89,
                strict_mode=True,
            )
            self.assertFalse(confirmed)
            self.assertIsNone(reason)

        confirmed, score, reason = service._confirm_match_candidate(
            camera_id="cam-1",
            worker_id=1,
            score=0.89,
            strict_mode=True,
        )
        self.assertTrue(confirmed)
        self.assertIsNone(reason)
        self.assertGreaterEqual(score, 0.89)

    def test_strict_confirmation_mode_rejects_weak_repeated_scores(self) -> None:
        service = object.__new__(ScalableAttendanceService)
        service.worker_profiles = {
            1: WorkerProfile(
                centroid=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
                min_similarity=0.9,
                mean_similarity=0.94,
                sample_count=3,
            )
        }
        service.pending_matches = {}

        scores = [0.88, 0.87, 0.86, 0.85, 0.80]
        result = (False, 0.0, None)
        for value in scores:
            result = service._confirm_match_candidate(
                camera_id="cam-1",
                worker_id=1,
                score=value,
                strict_mode=True,
            )

        confirmed, score, reason = result
        self.assertFalse(confirmed)
        self.assertIsNotNone(reason)
        self.assertIn("repeated face checks", reason)

    def test_multiple_face_result_requires_one_person_in_frame(self) -> None:
        service = object.__new__(ScalableAttendanceService)
        result = service._multiple_face_result([(0, 0, 100, 100), (120, 0, 100, 100)])

        self.assertEqual(result.detected_faces, 2)
        self.assertEqual(result.unknown_faces, 2)
        self.assertEqual(len(result.debug_faces), 2)
        self.assertIn("Only one employee", result.debug_faces[0].reason)


if __name__ == "__main__":
    unittest.main()
