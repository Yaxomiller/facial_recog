from __future__ import annotations

import unittest

import numpy as np

from src.v2.index import SearchHit
from src.v2.service import ScalableAttendanceService


class _StubEmbedder:
    def __init__(self, outputs: list[np.ndarray]) -> None:
        self._outputs = outputs
        self._index = 0

    def embed(self, _face_image: np.ndarray) -> np.ndarray:
        output = self._outputs[min(self._index, len(self._outputs) - 1)]
        self._index += 1
        return output


class _StubIndex:
    def __init__(self, results: list[list[SearchHit]]) -> None:
        self._results = results

    def batch_search(self, _embeddings: list[np.ndarray], top_k: int) -> list[list[SearchHit]]:
        return [result[:top_k] for result in self._results]


class DescriptorConsensusTests(unittest.TestCase):
    def _service(self, results: list[list[SearchHit]]) -> ScalableAttendanceService:
        service = object.__new__(ScalableAttendanceService)
        service.embedder = _StubEmbedder(
            [
                np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
                np.asarray([0.98, 0.02, 0.0], dtype=np.float32),
            ]
        )
        service.index = _StubIndex(results)
        return service

    def test_accepts_consistent_descriptor_variants(self) -> None:
        service = self._service(
            [
                [SearchHit(worker_id=1, score=0.88), SearchHit(worker_id=2, score=0.72)],
                [SearchHit(worker_id=1, score=0.86), SearchHit(worker_id=2, score=0.71)],
            ]
        )

        consensus = service._descriptor_consensus(np.zeros((120, 120, 3), dtype=np.uint8), top_k=2)

        self.assertEqual(consensus.worker_id, 1)
        self.assertIsNone(consensus.rejection_reason)
        self.assertGreaterEqual(consensus.variant_hits, 2)

    def test_rejects_disagreeing_descriptor_variants(self) -> None:
        service = self._service(
            [
                [SearchHit(worker_id=1, score=0.88), SearchHit(worker_id=2, score=0.72)],
                [SearchHit(worker_id=2, score=0.87), SearchHit(worker_id=1, score=0.73)],
            ]
        )

        consensus = service._descriptor_consensus(np.zeros((120, 120, 3), dtype=np.uint8), top_k=2)

        self.assertIsNone(consensus.worker_id)
        self.assertIsNotNone(consensus.rejection_reason)
        self.assertIn("disagreed", consensus.rejection_reason)


if __name__ == "__main__":
    unittest.main()
