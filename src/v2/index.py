from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from src.v2.config import ALLOW_BACKEND_FALLBACK, VECTOR_INDEX_BACKEND, VECTOR_INDEX_FILE


@dataclass
class SearchHit:
    worker_id: int
    score: float


class BaseVectorIndex(ABC):
    backend_name = "base"

    @abstractmethod
    def build(self, worker_ids: list[int], vectors: list[np.ndarray]) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: np.ndarray, top_k: int) -> list[SearchHit]:
        raise NotImplementedError

    def batch_search(self, queries: list[np.ndarray], top_k: int) -> list[list[SearchHit]]:
        return [self.search(query, top_k) for query in queries]

    @abstractmethod
    def save(self, namespace: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def load(self, expected_namespace: str | None = None) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def size(self) -> int:
        raise NotImplementedError


class NumpyVectorIndex(BaseVectorIndex):
    backend_name = "numpy"

    def __init__(self) -> None:
        self.worker_ids = np.empty((0,), dtype=np.int32)
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.namespace = ""

    def build(self, worker_ids: list[int], vectors: list[np.ndarray]) -> None:
        if not vectors:
            self.worker_ids = np.empty((0,), dtype=np.int32)
            self.vectors = np.empty((0, 0), dtype=np.float32)
            return

        matrix = np.vstack(vectors).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.vectors = matrix / norms
        self.worker_ids = np.asarray(worker_ids, dtype=np.int32)

    def search(self, query: np.ndarray, top_k: int) -> list[SearchHit]:
        if self.vectors.size == 0:
            return []

        query = query.astype(np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []

        normalized_query = query / query_norm
        scores = self.vectors @ normalized_query
        top_k = max(1, min(top_k, len(scores)))
        candidate_indexes = np.argpartition(scores, -top_k)[-top_k:]
        ranked_indexes = candidate_indexes[np.argsort(scores[candidate_indexes])[::-1]]
        return [SearchHit(worker_id=int(self.worker_ids[i]), score=float(scores[i])) for i in ranked_indexes]

    def batch_search(self, queries: list[np.ndarray], top_k: int) -> list[list[SearchHit]]:
        if self.vectors.size == 0 or not queries:
            return [[] for _ in queries]

        matrix = np.vstack([query.astype(np.float32) for query in queries])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized_queries = matrix / norms
        scores = normalized_queries @ self.vectors.T
        results: list[list[SearchHit]] = []
        for row in scores:
            effective_top_k = max(1, min(top_k, len(row)))
            candidate_indexes = np.argpartition(row, -effective_top_k)[-effective_top_k:]
            ranked_indexes = candidate_indexes[np.argsort(row[candidate_indexes])[::-1]]
            results.append([SearchHit(worker_id=int(self.worker_ids[i]), score=float(row[i])) for i in ranked_indexes])
        return results

    def save(self, namespace: str | None = None) -> None:
        VECTOR_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace or ""
        np.savez(
            VECTOR_INDEX_FILE,
            worker_ids=self.worker_ids,
            vectors=self.vectors,
            namespace=np.asarray(self.namespace),
        )

    def load(self, expected_namespace: str | None = None) -> bool:
        if not VECTOR_INDEX_FILE.exists():
            return False

        payload = np.load(VECTOR_INDEX_FILE)
        stored_namespace = str(payload["namespace"]) if "namespace" in payload else ""
        expected = expected_namespace or ""
        if stored_namespace != expected:
            return False
        self.worker_ids = payload["worker_ids"].astype(np.int32)
        self.vectors = payload["vectors"].astype(np.float32)
        self.namespace = stored_namespace
        return True

    @property
    def size(self) -> int:
        return int(len(self.worker_ids))


class LSHVectorIndex(BaseVectorIndex):
    backend_name = "lsh"

    def __init__(self, num_tables: int = 8, num_bits: int = 14, random_seed: int = 42) -> None:
        self.num_tables = num_tables
        self.num_bits = num_bits
        self.random_seed = random_seed
        self.worker_ids = np.empty((0,), dtype=np.int32)
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.hyperplanes = np.empty((0, 0, 0), dtype=np.float32)
        self.tables: list[dict[int, list[int]]] = []
        self.namespace = ""

    def build(self, worker_ids: list[int], vectors: list[np.ndarray]) -> None:
        if not vectors:
            self.worker_ids = np.empty((0,), dtype=np.int32)
            self.vectors = np.empty((0, 0), dtype=np.float32)
            self.hyperplanes = np.empty((0, 0, 0), dtype=np.float32)
            self.tables = []
            return

        matrix = np.vstack(vectors).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.vectors = matrix / norms
        self.worker_ids = np.asarray(worker_ids, dtype=np.int32)

        rng = np.random.default_rng(self.random_seed)
        self.hyperplanes = rng.standard_normal(
            (self.num_tables, self.num_bits, self.vectors.shape[1]),
            dtype=np.float32,
        )
        self.tables = [dict() for _ in range(self.num_tables)]
        for index, vector in enumerate(self.vectors):
            for table_index in range(self.num_tables):
                hash_value = self._hash_vector(vector, table_index)
                self.tables[table_index].setdefault(hash_value, []).append(index)

    def search(self, query: np.ndarray, top_k: int) -> list[SearchHit]:
        if self.vectors.size == 0:
            return []

        query = query.astype(np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []

        normalized_query = query / query_norm
        candidate_indexes = self._candidate_indexes(normalized_query)
        if not candidate_indexes:
            candidate_indexes = list(range(len(self.worker_ids)))

        candidate_array = np.asarray(candidate_indexes, dtype=np.int32)
        candidate_vectors = self.vectors[candidate_array]
        scores = candidate_vectors @ normalized_query
        effective_top_k = max(1, min(top_k, len(scores)))
        local_indexes = np.argpartition(scores, -effective_top_k)[-effective_top_k:]
        ranked_local_indexes = local_indexes[np.argsort(scores[local_indexes])[::-1]]
        return [
            SearchHit(
                worker_id=int(self.worker_ids[candidate_array[i]]),
                score=float(scores[i]),
            )
            for i in ranked_local_indexes
        ]

    def save(self, namespace: str | None = None) -> None:
        VECTOR_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace or ""
        np.savez(
            VECTOR_INDEX_FILE,
            worker_ids=self.worker_ids,
            vectors=self.vectors,
            hyperplanes=self.hyperplanes,
            namespace=np.asarray(self.namespace),
        )

    def load(self, expected_namespace: str | None = None) -> bool:
        if not VECTOR_INDEX_FILE.exists():
            return False

        payload = np.load(VECTOR_INDEX_FILE)
        stored_namespace = str(payload["namespace"]) if "namespace" in payload else ""
        expected = expected_namespace or ""
        if stored_namespace != expected:
            return False

        self.worker_ids = payload["worker_ids"].astype(np.int32)
        self.vectors = payload["vectors"].astype(np.float32)
        self.hyperplanes = payload["hyperplanes"].astype(np.float32) if "hyperplanes" in payload else np.empty((0, 0, 0), dtype=np.float32)
        self.namespace = stored_namespace
        self.tables = [dict() for _ in range(len(self.hyperplanes))]
        for index, vector in enumerate(self.vectors):
            for table_index in range(len(self.hyperplanes)):
                hash_value = self._hash_vector(vector, table_index)
                self.tables[table_index].setdefault(hash_value, []).append(index)
        return True

    def _candidate_indexes(self, normalized_query: np.ndarray) -> list[int]:
        candidates: set[int] = set()
        for table_index in range(len(self.tables)):
            hash_value = self._hash_vector(normalized_query, table_index)
            candidates.update(self.tables[table_index].get(hash_value, []))
        return list(candidates)

    def _hash_vector(self, vector: np.ndarray, table_index: int) -> int:
        projections = self.hyperplanes[table_index] @ vector
        bits = (projections >= 0).astype(np.uint8)
        hash_value = 0
        for bit in bits:
            hash_value = (hash_value << 1) | int(bit)
        return hash_value

    @property
    def size(self) -> int:
        return int(len(self.worker_ids))


class FaissVectorIndex(BaseVectorIndex):
    backend_name = "faiss"

    def __init__(self) -> None:
        try:
            import faiss  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "FAISS backend requested but `faiss` is not installed. "
                "Install faiss-cpu/faiss-gpu or switch ATTENDANCE_VECTOR_INDEX_BACKEND to numpy."
            ) from exc

        self._faiss = faiss
        self.worker_ids = np.empty((0,), dtype=np.int32)
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.index = None
        self.namespace = ""

    def build(self, worker_ids: list[int], vectors: list[np.ndarray]) -> None:
        if not vectors:
            self.worker_ids = np.empty((0,), dtype=np.int32)
            self.vectors = np.empty((0, 0), dtype=np.float32)
            self.index = None
            return

        matrix = np.vstack(vectors).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized = matrix / norms
        self.worker_ids = np.asarray(worker_ids, dtype=np.int32)
        self.vectors = normalized
        self.index = self._faiss.IndexFlatIP(normalized.shape[1])
        self.index.add(normalized)

    def search(self, query: np.ndarray, top_k: int) -> list[SearchHit]:
        if self.index is None or self.worker_ids.size == 0:
            return []

        query = query.astype(np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []

        normalized_query = (query / query_norm).reshape(1, -1)
        top_k = max(1, min(top_k, len(self.worker_ids)))
        scores, indexes = self.index.search(normalized_query, top_k)
        hits: list[SearchHit] = []
        for score, idx in zip(scores[0], indexes[0]):
            if idx < 0:
                continue
            hits.append(SearchHit(worker_id=int(self.worker_ids[idx]), score=float(score)))
        return hits

    def batch_search(self, queries: list[np.ndarray], top_k: int) -> list[list[SearchHit]]:
        if self.index is None or self.worker_ids.size == 0 or not queries:
            return [[] for _ in queries]

        matrix = np.vstack([query.astype(np.float32) for query in queries])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized_queries = matrix / norms
        effective_top_k = max(1, min(top_k, len(self.worker_ids)))
        scores, indexes = self.index.search(normalized_queries, effective_top_k)
        results: list[list[SearchHit]] = []
        for row_scores, row_indexes in zip(scores, indexes):
            hits: list[SearchHit] = []
            for score, idx in zip(row_scores, row_indexes):
                if idx < 0:
                    continue
                hits.append(SearchHit(worker_id=int(self.worker_ids[idx]), score=float(score)))
            results.append(hits)
        return results

    def save(self, namespace: str | None = None) -> None:
        VECTOR_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace or ""
        np.savez(
            VECTOR_INDEX_FILE,
            worker_ids=self.worker_ids,
            vectors=self.vectors,
            namespace=np.asarray(self.namespace),
        )

    def load(self, expected_namespace: str | None = None) -> bool:
        if not VECTOR_INDEX_FILE.exists():
            return False

        payload = np.load(VECTOR_INDEX_FILE)
        stored_namespace = str(payload["namespace"]) if "namespace" in payload else ""
        expected = expected_namespace or ""
        if stored_namespace != expected:
            return False
        self.worker_ids = payload["worker_ids"].astype(np.int32)
        self.vectors = payload["vectors"].astype(np.float32)
        self.namespace = stored_namespace
        if self.vectors.size == 0:
            self.index = None
            return True

        self.index = self._faiss.IndexFlatIP(self.vectors.shape[1])
        self.index.add(self.vectors)
        return True

    @property
    def size(self) -> int:
        return int(len(self.worker_ids))


def build_vector_index() -> BaseVectorIndex:
    backend = VECTOR_INDEX_BACKEND.lower().strip()
    if backend == "lsh":
        return LSHVectorIndex()
    if backend == "faiss":
        try:
            return FaissVectorIndex()
        except RuntimeError:
            if not ALLOW_BACKEND_FALLBACK:
                raise
            return NumpyVectorIndex()
    return NumpyVectorIndex()


def resolve_index() -> tuple[BaseVectorIndex, str, str, list[str]]:
    backend = VECTOR_INDEX_BACKEND.lower().strip()
    warnings: list[str] = []
    index = build_vector_index()
    if backend != index.backend_name:
        warnings.append(
            f"Requested vector index backend '{backend}' is unavailable. Falling back to '{index.backend_name}'."
        )
    return index, backend, index.backend_name, warnings
