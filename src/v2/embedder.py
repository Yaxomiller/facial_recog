from __future__ import annotations

from abc import ABC, abstractmethod

import cv2
import numpy as np

from src.v2.config import ALLOW_BACKEND_FALLBACK, EMBEDDING_BACKEND, EMBEDDING_SIZE, FACE_SIZE
from src.v2.vision import align_face_by_eyes


class BaseFaceEmbedder(ABC):
    name = "base"
    production_ready = False
    vector_size: int | None = None

    @abstractmethod
    def embed(self, face_image: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class HistogramFaceEmbedder(BaseFaceEmbedder):
    """
    Lightweight fallback embedder.

    This is suitable for local development and API plumbing, not high-scale production.
    """

    name = "histogram"
    production_ready = False
    vector_size = 304

    def __init__(self) -> None:
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def embed(self, face_image: np.ndarray) -> np.ndarray:
        normalized = self._prepare_face(face_image)
        global_hist = self._normalize_part(
            cv2.calcHist([normalized], [0], None, [32], [0, 256]).flatten().astype(np.float32)
        )

        regional_parts: list[np.ndarray] = []
        for region in self._split_grid(normalized, rows=2, cols=2):
            regional_parts.append(
                self._normalize_part(
                    cv2.calcHist([region], [0], None, [16], [0, 256]).flatten().astype(np.float32)
                )
            )
        regional_hist = np.concatenate(regional_parts, axis=0)

        gradient_hist = self._normalize_part(self._gradient_histogram(normalized, rows=4, cols=4, bins=8))
        lbp_hist = self._normalize_part(self._lbp_histogram(normalized, bins=32))
        dct_features = self._normalize_part(self._low_frequency_dct(normalized, rows=7, cols=7))

        descriptor = np.concatenate([global_hist, regional_hist, gradient_hist, lbp_hist, dct_features]).astype(np.float32)
        descriptor = np.sqrt(np.clip(descriptor, 0.0, None))
        norm = np.linalg.norm(descriptor)
        if norm == 0:
            return descriptor
        return descriptor / norm

    def _prepare_face(self, face_image: np.ndarray) -> np.ndarray:
        aligned, _eyes_detected = align_face_by_eyes(face_image, FACE_SIZE)
        normalized = self._clahe.apply(aligned)
        denoised = cv2.bilateralFilter(normalized, 5, 18, 18)
        sharpened = cv2.addWeighted(denoised, 1.2, cv2.GaussianBlur(denoised, (0, 0), 1.1), -0.2, 0)
        mask = np.zeros_like(sharpened)
        center = (sharpened.shape[1] // 2, sharpened.shape[0] // 2)
        axes = (int(sharpened.shape[1] * 0.42), int(sharpened.shape[0] * 0.48))
        cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
        return cv2.bitwise_and(sharpened, mask)

    def _split_grid(self, image: np.ndarray, rows: int, cols: int) -> list[np.ndarray]:
        height, width = image.shape[:2]
        row_edges = np.linspace(0, height, rows + 1, dtype=int)
        col_edges = np.linspace(0, width, cols + 1, dtype=int)
        cells: list[np.ndarray] = []
        for row_index in range(rows):
            for col_index in range(cols):
                top, bottom = row_edges[row_index], row_edges[row_index + 1]
                left, right = col_edges[col_index], col_edges[col_index + 1]
                cells.append(image[top:bottom, left:right])
        return cells

    def _gradient_histogram(self, image: np.ndarray, rows: int, cols: int, bins: int) -> np.ndarray:
        grad_x = cv2.Sobel(image, cv2.CV_32F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(image, cv2.CV_32F, 0, 1, ksize=3)
        magnitude, angle = cv2.cartToPolar(grad_x, grad_y, angleInDegrees=True)
        angle = np.mod(angle, 180.0)
        row_edges = np.linspace(0, image.shape[0], rows + 1, dtype=int)
        col_edges = np.linspace(0, image.shape[1], cols + 1, dtype=int)
        descriptor: list[np.ndarray] = []

        for row_index in range(rows):
            for col_index in range(cols):
                top, bottom = row_edges[row_index], row_edges[row_index + 1]
                left, right = col_edges[col_index], col_edges[col_index + 1]
                cell_angles = angle[top:bottom, left:right].reshape(-1)
                cell_magnitude = magnitude[top:bottom, left:right].reshape(-1)
                hist, _ = np.histogram(
                    cell_angles,
                    bins=bins,
                    range=(0.0, 180.0),
                    weights=cell_magnitude,
                )
                descriptor.append(hist.astype(np.float32))

        return np.concatenate(descriptor, axis=0)

    def _lbp_histogram(self, image: np.ndarray, bins: int) -> np.ndarray:
        center = image[1:-1, 1:-1]
        lbp = np.zeros_like(center, dtype=np.uint8)
        neighbors = [
            image[:-2, :-2],
            image[:-2, 1:-1],
            image[:-2, 2:],
            image[1:-1, 2:],
            image[2:, 2:],
            image[2:, 1:-1],
            image[2:, :-2],
            image[1:-1, :-2],
        ]

        for bit_index, neighbor in enumerate(neighbors):
            lbp |= ((neighbor >= center).astype(np.uint8) << bit_index)

        grouped = (lbp.astype(np.int32) * bins) // 256
        hist, _ = np.histogram(grouped, bins=bins, range=(0, bins))
        return hist.astype(np.float32)

    def _low_frequency_dct(self, image: np.ndarray, rows: int, cols: int) -> np.ndarray:
        normalized = image.astype(np.float32) / 255.0
        coefficients = cv2.dct(normalized)
        low_frequency = coefficients[:rows, :cols].flatten()
        if low_frequency.size <= 1:
            return low_frequency.astype(np.float32)
        return low_frequency[1:].astype(np.float32)

    def _normalize_part(self, vector: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(vector)
        if norm == 0:
            return vector
        return vector / norm


class LBPHFaceEmbedder(HistogramFaceEmbedder):
    """
    Practical Windows-friendly backend.

    Uses OpenCV LBPH for recognition in the service layer while keeping the
    richer histogram/gradient/LBP descriptor for open-set verification.
    """

    name = "lbph"
    production_ready = True


class ClassicalFaceEmbedder(HistogramFaceEmbedder):
    name = "classical"
    production_ready = True


def build_embedder() -> BaseFaceEmbedder:
    backend = EMBEDDING_BACKEND.lower().strip()
    if backend == "classical":
        return ClassicalFaceEmbedder()
    if backend == "lbph":
        return LBPHFaceEmbedder()
    if backend == "histogram":
        return ClassicalFaceEmbedder()
    if not ALLOW_BACKEND_FALLBACK:
        raise RuntimeError(f"Unsupported embedder backend '{backend}'. Supported backends: classical, lbph.")
    return ClassicalFaceEmbedder()


def resolve_embedder() -> tuple[BaseFaceEmbedder, str, str, list[str]]:
    backend = EMBEDDING_BACKEND.lower().strip()
    warnings: list[str] = []
    try:
        embedder = build_embedder()
    except Exception as exc:
        if not ALLOW_BACKEND_FALLBACK:
            raise
        embedder = ClassicalFaceEmbedder()
        warnings.append(
            f"Requested embedder backend '{backend}' failed to initialize ({exc}). Falling back to '{embedder.name}'."
        )
    if backend != embedder.name:
        warnings.append(
            f"Requested embedder backend '{backend}' is unavailable. Falling back to '{embedder.name}'."
        )
    return embedder, backend, embedder.name, warnings
