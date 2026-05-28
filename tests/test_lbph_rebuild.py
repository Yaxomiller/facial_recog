from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from src.v2.service import ScalableAttendanceService


class _StubEmbedder:
    name = "lbph"
    vector_size = 3

    def embed(self, _image: np.ndarray) -> np.ndarray:
        raise AssertionError("LBPH rebuild should reuse stored enrollment vectors instead of recomputing them.")


class _StubIndex:
    backend_name = "numpy"

    def __init__(self) -> None:
        self.built_worker_ids: list[int] = []
        self.built_vectors: list[np.ndarray] = []
        self.saved_namespace = ""

    def build(self, worker_ids: list[int], vectors: list[np.ndarray]) -> None:
        self.built_worker_ids = list(worker_ids)
        self.built_vectors = [vector.copy() for vector in vectors]

    def save(self, namespace: str | None = None) -> None:
        self.saved_namespace = namespace or ""

    @property
    def size(self) -> int:
        return len(self.built_worker_ids)


class _StubRecognizer:
    def __init__(self) -> None:
        self.trained_faces: list[np.ndarray] = []
        self.trained_labels: np.ndarray | None = None

    def train(self, faces: list[np.ndarray], labels: np.ndarray) -> None:
        self.trained_faces = list(faces)
        self.trained_labels = labels.copy()


class LbphRebuildTests(unittest.TestCase):
    def test_rebuild_uses_stored_vectors_for_descriptor_index(self) -> None:
        stored_vector = np.asarray([0.1, 0.2, 0.3], dtype=np.float32)
        recognizer = _StubRecognizer()
        service = object.__new__(ScalableAttendanceService)
        service.embedder = _StubEmbedder()
        service.index = _StubIndex()
        service.worker_profiles = {}
        service.lbph_recognizer = None
        service.lbph_label_to_worker_id = {}

        with (
            patch("src.v2.service.repository.fetch_training_samples", return_value=[(7, stored_vector, b"face-bytes")]),
            patch("src.v2.service.repository.fetch_embeddings", return_value=[]),
            patch("src.v2.service.repository.worker_count", return_value=1),
            patch("src.v2.service.cv2.imdecode", return_value=np.zeros((112, 112), dtype=np.uint8)),
            patch("src.v2.service.cv2.face.LBPHFaceRecognizer_create", return_value=recognizer),
        ):
            stats = ScalableAttendanceService._rebuild_lbph_model(service)

        self.assertEqual(stats.indexed_workers, 1)
        self.assertEqual(stats.indexed_embeddings, 1)
        self.assertEqual(service.index.built_worker_ids, [7])
        np.testing.assert_array_equal(service.index.built_vectors[0], stored_vector)
        self.assertIs(service.lbph_recognizer, recognizer)
        self.assertEqual(service.lbph_label_to_worker_id, {0: 7})
        self.assertEqual(len(recognizer.trained_faces), 1)

    def test_initialize_state_raises_clear_error_when_rebuild_and_load_fail(self) -> None:
        service = object.__new__(ScalableAttendanceService)
        service.warnings = []
        service.embedder = type("Embedder", (), {"name": "lbph", "vector_size": 3})()
        service.index = type(
            "Index",
            (),
            {
                "backend_name": "numpy",
                "load": lambda self, expected_namespace=None: False,
            },
        )()
        service.rebuild_index = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        service._index_namespace = lambda: "namespace"

        with self.assertRaisesRegex(RuntimeError, "no compatible saved index"):
            ScalableAttendanceService._initialize_recognition_state(service)


if __name__ == "__main__":
    unittest.main()
