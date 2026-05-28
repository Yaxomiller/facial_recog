from __future__ import annotations

import unittest

import numpy as np

from src.v2.embedder import HistogramFaceEmbedder
from src.v2.vision import select_eye_pair


class FacePreprocessingTests(unittest.TestCase):
    def test_select_eye_pair_prefers_wide_level_pair(self) -> None:
        eyes = [
            (10, 18, 16, 12),
            (68, 19, 15, 12),
            (42, 42, 14, 10),
        ]

        pair = select_eye_pair(eyes, image_width=112, image_height=112)

        self.assertIsNotNone(pair)
        left_eye, right_eye = pair or ((0.0, 0.0), (0.0, 0.0))
        self.assertLess(left_eye[0], right_eye[0])
        self.assertLess(abs(left_eye[1] - right_eye[1]), 6.0)

    def test_histogram_embedder_keeps_expected_vector_size(self) -> None:
        embedder = HistogramFaceEmbedder()
        image = np.full((140, 140, 3), 128, dtype=np.uint8)
        descriptor = embedder.embed(image)

        self.assertEqual(descriptor.shape[0], embedder.vector_size)
        self.assertEqual(embedder.vector_size, 304)


if __name__ == "__main__":
    unittest.main()
