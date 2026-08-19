from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

import cv2
import numpy as np

from src.breath_analyzer import (
    alcohol_bac_percent,
    cannabis_confidence_score,
    resolve_breath_analyzer,
)
from src.v2 import repository
from src.v2.cache import RecognitionCache
from src.v2.config import (
    ALLOW_BACKEND_FALLBACK,
    AMBIGUITY_MARGIN,
    BREATH_ANALYZER_MODE,
    DEFAULT_LIST_LIMIT,
    FACE_SIZE,
    LBPH_CONFIDENCE_THRESHOLD,
    MAX_PROFILE_CENTROID_THRESHOLD,
    MIN_ENROLLMENT_IMAGES,
    MATCH_THRESHOLD,
    OPEN_SET_CENTROID_MARGIN,
    DESCRIPTOR_VARIANT_REQUIRED_HITS,
    OPEN_SET_MIN_CENTROID_SCORE,
    OPEN_SET_MIN_SCORE,
    OPEN_SET_SUPPORT_SCORE,
    SINGLE_PROFILE_LBPH_CONFIDENCE_THRESHOLD,
    SINGLE_PROFILE_MIN_CENTROID_SCORE,
    SINGLE_PROFILE_MIN_SCORE,
    SINGLE_PROFILE_REQUIRED_SUPPORT_HITS,
    SINGLE_PROFILE_SUPPORT_SCORE,
    MIN_FACE_BLUR_VARIANCE,
    MIN_FACE_HEIGHT,
    MIN_FACE_WIDTH,
    MAX_FACES_PER_REQUEST,
    MAX_TOP_K,
    RECOGNITION_CACHE_TTL_SECONDS,
)
from src.v2.embedder import resolve_embedder
from src.v2.index import resolve_index
from src.v2.schemas import (
    ArchitectureNote,
    AttendanceRow,
    BreathTestSessionCancelResult,
    BreathTestSessionStartResult,
    BreathTestResult,
    CandidateDebug,
    DeleteAttendanceResult,
    DeleteWorkerResult,
    DetectionBox,
    DetectionResult,
    EnrollmentResult,
    FaceDebug,
    IndexStats,
    MatchResult,
    RecognitionResult,
    ServiceStatus,
    WorkerRead,
)
from src.v2.vision import align_face_by_eyes, detect_faces, detector_backend_name, expand_face_box


def _event_float(event: Any, column: str) -> float:
    """Read an optional numeric column from a sqlite3.Row. Rows created before
    the breath-detail migration simply do not carry these columns."""
    try:
        value = event[column]
    except (IndexError, KeyError):
        return 0.0
    return 0.0 if value is None else float(value)


@dataclass
class WorkerProfile:
    centroid: np.ndarray
    min_similarity: float
    mean_similarity: float
    sample_count: int


@dataclass
class PendingBreathSession:
    session_id: str
    worker_id: int
    camera_id: str
    started_at: datetime
    sample_seconds: float
    future: Future
    canceled: bool = False
    completed: bool = False
    # Total measurement wall time (purge + baseline + blow). The reading is
    # only ready after this long, so it drives the completion timeout.
    cycle_seconds: float = 0.0


@dataclass
class DescriptorConsensus:
    worker_id: Optional[int]
    best_score: float
    second_score: float
    support_scores: list[float]
    representative_embedding: Optional[np.ndarray]
    variant_hits: int
    candidates: list[CandidateDebug]
    rejection_reason: Optional[str] = None


class ScalableAttendanceService:
    def __init__(self) -> None:
        repository.init_schema()
        self.embedder, self.requested_embedder, self.active_embedder, embedder_warnings = resolve_embedder()
        self.index, self.requested_index, self.active_index, index_warnings = resolve_index()
        self.warnings = embedder_warnings + index_warnings
        self.recognition_cache = RecognitionCache(ttl_seconds=RECOGNITION_CACHE_TTL_SECONDS)
        self.breath_analyzer = resolve_breath_analyzer()
        self.warnings.extend(getattr(self.breath_analyzer, "startup_warnings", ()))
        self.breath_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="breath-analyzer")
        self.breath_session_lock = Lock()
        self.breath_sessions: dict[str, PendingBreathSession] = {}
        self.lbph_recognizer: Optional[cv2.face_LBPHFaceRecognizer] = None
        self.lbph_label_to_worker_id: dict[int, int] = {}
        self.worker_profiles: dict[int, WorkerProfile] = {}
        self._initialize_recognition_state()

    def _initialize_recognition_state(self) -> None:
        rebuild_error: Optional[Exception] = None
        try:
            # Rebuild from the current enrollment database on every startup so
            # LBPH state, vector hits, and worker profiles cannot drift apart.
            self.rebuild_index()
            return
        except Exception as exc:
            rebuild_error = exc
            self.warnings.append(
                "Recognition startup rebuild failed; falling back to the last saved vector index "
                f"({exc}). Re-enroll or rebuild the index after restoring Python/OpenCV if recognition looks stale."
            )

        if self.index.load(expected_namespace=self._index_namespace()):
            raw_embeddings = self._load_profile_embeddings()
            self.worker_profiles = self._build_worker_profiles(raw_embeddings)
            return

        if rebuild_error is not None:
            raise RuntimeError("Recognition startup failed and no compatible saved index could be loaded.") from rebuild_error
        raise RuntimeError("Recognition startup failed because no compatible saved index could be loaded.")

    def _index_namespace(self) -> str:
        return f"embedder:{self.embedder.name}:dim:{self.embedder.vector_size}:index:{self.index.backend_name}"

    def _load_profile_embeddings(self) -> list[tuple[int, np.ndarray]]:
        raw_embeddings = repository.fetch_embeddings(
            backend=self.embedder.name,
            dimension=self.embedder.vector_size,
        )
        if raw_embeddings or self.embedder.name != "lbph":
            return raw_embeddings
        return repository.fetch_embeddings(
            backend="classical",
            dimension=self.embedder.vector_size,
        )

    def _confirm_match_candidate(
        self,
        camera_id: str,
        worker_id: int,
        score: float,
    ) -> tuple[bool, float, Optional[str]]:
        # A candidate reaching this point has already passed the open-set
        # gate, ambiguity margin, and LBPH/descriptor agreement checks for
        # this single frame, so it is accepted immediately rather than
        # waiting for repeated confirmation across multiple frames.
        return True, score, None

    def _multiple_face_result(self, faces: list[tuple[int, int, int, int]]) -> RecognitionResult:
        return RecognitionResult(
            matches=[],
            unknown_faces=len(faces),
            detected_faces=len(faces),
            boxes=[DetectionBox(x=x, y=y, width=w, height=h) for (x, y, w, h) in faces],
            debug_faces=[
                FaceDebug(
                    face_index=face_index,
                    accepted=False,
                    reason="Multiple faces detected. Only one employee can scan at a time.",
                )
                for face_index, _face in enumerate(faces)
            ],
        )

    def _tighten_face_crop(self, face_crop: np.ndarray, trim_ratio: float = 0.08) -> Optional[np.ndarray]:
        height, width = face_crop.shape[:2]
        trim_x = int(width * trim_ratio)
        trim_y = int(height * trim_ratio)
        if trim_x <= 0 or trim_y <= 0:
            return None
        if (width - (trim_x * 2)) < 48 or (height - (trim_y * 2)) < 48:
            return None
        return face_crop[trim_y : height - trim_y, trim_x : width - trim_x]

    def _descriptor_consensus(self, face_crop: np.ndarray, top_k: int) -> DescriptorConsensus:
        variants = [face_crop]
        tighter = self._tighten_face_crop(face_crop)
        if tighter is not None:
            variants.append(tighter)

        embeddings = [self.embedder.embed(variant) for variant in variants]
        search_results = self.index.batch_search(embeddings, top_k=top_k)
        if not any(search_results):
            return DescriptorConsensus(
                worker_id=None,
                best_score=0.0,
                second_score=0.0,
                support_scores=[],
                representative_embedding=None,
                variant_hits=0,
                candidates=[],
                rejection_reason="Rejected: no similar enrolled face found.",
            )

        worker_variant_best_scores: dict[int, list[float]] = defaultdict(list)
        worker_support_scores: dict[int, list[float]] = defaultdict(list)
        worker_max_scores: dict[int, float] = {}
        variant_winner_worker_ids: list[int] = []
        variant_winner_embeddings: list[np.ndarray] = []
        variant_winner_scores: list[float] = []

        for embedding, hits in zip(embeddings, search_results):
            if not hits:
                continue

            grouped_scores: dict[int, float] = {}
            for hit in hits:
                score = float(hit.score)
                grouped_scores[hit.worker_id] = max(grouped_scores.get(hit.worker_id, 0.0), score)
                worker_support_scores[hit.worker_id].append(score)
                worker_max_scores[hit.worker_id] = max(worker_max_scores.get(hit.worker_id, 0.0), score)

            if not grouped_scores:
                continue

            ranked_scores = sorted(grouped_scores.items(), key=lambda item: item[1], reverse=True)
            winner_worker_id, winner_score = ranked_scores[0]
            worker_variant_best_scores[winner_worker_id].append(float(winner_score))
            variant_winner_worker_ids.append(winner_worker_id)
            variant_winner_embeddings.append(embedding)
            variant_winner_scores.append(float(winner_score))

        if not worker_variant_best_scores:
            return DescriptorConsensus(
                worker_id=None,
                best_score=0.0,
                second_score=0.0,
                support_scores=[],
                representative_embedding=None,
                variant_hits=0,
                candidates=[],
                rejection_reason="Rejected: descriptor verification returned no stable candidate.",
            )

        ranked_candidates: list[tuple[int, int, float, float]] = []
        for worker_id, scores in worker_variant_best_scores.items():
            ranked_candidates.append(
                (
                    worker_id,
                    len(scores),
                    float(np.mean(scores)),
                    worker_max_scores.get(worker_id, 0.0),
                )
            )
        ranked_candidates.sort(key=lambda item: (item[1], item[2], item[3]), reverse=True)

        best_worker_id, variant_hits, average_variant_score, peak_score = ranked_candidates[0]
        second_score = ranked_candidates[1][2] if len(ranked_candidates) > 1 else 0.0
        representative_embedding = None
        best_embedding_score = float("-inf")
        for embedding, winner_worker_id, winner_score in zip(
            variant_winner_embeddings,
            variant_winner_worker_ids,
            variant_winner_scores,
        ):
            if winner_worker_id != best_worker_id:
                continue
            if winner_score > best_embedding_score:
                representative_embedding = embedding.astype(np.float32)
                best_embedding_score = winner_score

        candidates = [
            CandidateDebug(worker_id=worker_id, score=max(avg_score, peak))
            for worker_id, _votes, avg_score, peak in ranked_candidates[:3]
        ]

        if len(ranked_candidates) > 1 and ranked_candidates[1][1] == variant_hits:
            return DescriptorConsensus(
                worker_id=None,
                best_score=max(average_variant_score, peak_score),
                second_score=second_score,
                support_scores=worker_support_scores.get(best_worker_id, []),
                representative_embedding=representative_embedding,
                variant_hits=variant_hits,
                candidates=candidates,
                rejection_reason="Rejected: repeated descriptor checks disagreed on the employee identity.",
            )

        if variant_hits < min(DESCRIPTOR_VARIANT_REQUIRED_HITS, len(variants)):
            return DescriptorConsensus(
                worker_id=None,
                best_score=max(average_variant_score, peak_score),
                second_score=second_score,
                support_scores=worker_support_scores.get(best_worker_id, []),
                representative_embedding=representative_embedding,
                variant_hits=variant_hits,
                candidates=candidates,
                rejection_reason="Rejected: face match was not stable across repeated descriptor checks.",
            )

        return DescriptorConsensus(
            worker_id=best_worker_id,
            best_score=max(average_variant_score, peak_score),
            second_score=second_score,
            support_scores=worker_support_scores.get(best_worker_id, []),
            representative_embedding=representative_embedding,
            variant_hits=variant_hits,
            candidates=candidates,
            rejection_reason=None,
        )

    def _build_worker_profiles(self, raw_embeddings: list[tuple[int, np.ndarray]]) -> dict[int, WorkerProfile]:
        grouped: dict[int, list[np.ndarray]] = defaultdict(list)
        for worker_id, vector in raw_embeddings:
            normalized = vector.astype(np.float32)
            norm = np.linalg.norm(normalized)
            if norm != 0:
                normalized = normalized / norm
            grouped[worker_id].append(normalized)

        profiles: dict[int, WorkerProfile] = {}
        for worker_id, vectors in grouped.items():
            matrix = np.vstack(vectors).astype(np.float32)
            centroid = matrix.mean(axis=0).astype(np.float32)
            centroid_norm = np.linalg.norm(centroid)
            if centroid_norm != 0:
                centroid = centroid / centroid_norm
            similarities = matrix @ centroid
            profiles[worker_id] = WorkerProfile(
                centroid=centroid,
                min_similarity=float(np.min(similarities)),
                mean_similarity=float(np.mean(similarities)),
                sample_count=len(vectors),
            )
        return profiles

    def _score_progress(self, value: float, floor: float, ceiling: float) -> float:
        if ceiling <= floor:
            return 1.0 if value >= ceiling else 0.0
        return float(max(0.0, min(1.0, (value - floor) / (ceiling - floor))))

    def _single_profile_min_score(self, profile: Optional[WorkerProfile]) -> float:
        baseline = max(MATCH_THRESHOLD, OPEN_SET_MIN_SCORE)
        if profile is None:
            return float(max(baseline, min(SINGLE_PROFILE_MIN_SCORE, 0.78)))

        adaptive_floor = max(
            baseline,
            min(SINGLE_PROFILE_MIN_SCORE, profile.mean_similarity - 0.16),
        )
        return float(min(0.78, adaptive_floor))

    def _adaptive_profile_score_threshold(self, profile: Optional[WorkerProfile], profile_count: int) -> float:
        baseline = max(MATCH_THRESHOLD, OPEN_SET_MIN_SCORE)
        if profile is None:
            return float(baseline)
        if profile_count <= 1:
            return self._single_profile_min_score(profile)

        adaptive_floor = max(
            baseline,
            min(0.86, profile.mean_similarity - 0.18),
        )
        return float(adaptive_floor)

    def _adaptive_support_threshold(self, profile: Optional[WorkerProfile], profile_count: int) -> float:
        baseline = OPEN_SET_SUPPORT_SCORE
        if profile is None:
            return float(baseline)
        if profile_count <= 1:
            return float(max(baseline, SINGLE_PROFILE_SUPPORT_SCORE, min(0.82, profile.mean_similarity - 0.18)))
        return float(max(baseline, min(0.82, profile.mean_similarity - 0.22)))

    def _calibrated_match_confidence(
        self,
        worker_id: int,
        raw_score: float,
        support_scores: list[float],
        query_embedding: np.ndarray,
        auxiliary_score: Optional[float] = None,
    ) -> float:
        profile = self.worker_profiles.get(worker_id)
        profile_count = max(0, len(self.worker_profiles))
        score_floor = self._adaptive_profile_score_threshold(profile, profile_count)
        support_floor = self._adaptive_support_threshold(profile, profile_count)
        centroid_floor = OPEN_SET_MIN_CENTROID_SCORE

        if profile_count <= 1:
            centroid_floor = SINGLE_PROFILE_MIN_CENTROID_SCORE
            profile_mean = profile.mean_similarity if profile is not None else (score_floor + 0.08)
            score_target = min(0.79, max(score_floor + 0.05, profile_mean - 0.12))
            centroid_target = min(0.84, max(centroid_floor + 0.06, profile_mean - 0.08))
            support_target = min(0.80, max(support_floor + 0.05, score_target))
        else:
            profile_mean = profile.mean_similarity if profile is not None else 0.86
            score_target = min(0.86, max(score_floor + 0.08, profile_mean - 0.08))
            centroid_target = min(0.87, max(centroid_floor + 0.06, profile_mean - 0.06))
            support_target = min(0.84, max(support_floor + 0.06, score_target - 0.02))

        raw_component = self._score_progress(raw_score, score_floor, score_target)
        if support_scores:
            strongest_support = sorted((float(score) for score in support_scores), reverse=True)[:3]
            support_anchor = float(np.mean(strongest_support))
        else:
            support_anchor = raw_score
        support_component = self._score_progress(support_anchor, support_floor, support_target)

        centroid_component = raw_component
        if profile is not None:
            normalized_query = query_embedding.astype(np.float32)
            query_norm = np.linalg.norm(normalized_query)
            if query_norm != 0:
                normalized_query = normalized_query / query_norm
                centroid_score = float(normalized_query @ profile.centroid)
                centroid_component = self._score_progress(centroid_score, centroid_floor, centroid_target)

        blended_component = (
            (raw_component * 0.45)
            + (support_component * 0.20)
            + (centroid_component * 0.35)
        )
        confidence = 0.64 + (0.34 * blended_component)

        if auxiliary_score is not None:
            auxiliary_component = self._score_progress(auxiliary_score, 0.60, 0.88)
            confidence = max(confidence, 0.62 + (0.32 * auxiliary_component))

        return float(max(0.0, min(0.99, confidence)))

    def _passes_open_set_gate(
        self,
        worker_id: int,
        best_score: float,
        support_scores: list[float],
        query_embedding: np.ndarray,
    ) -> tuple[bool, str]:
        profile = self.worker_profiles.get(worker_id)
        if profile is not None and profile.sample_count < MIN_ENROLLMENT_IMAGES:
            return (
                False,
                f"Rejected: employee has only {profile.sample_count} enrolled face sample(s). Re-enroll with at least {MIN_ENROLLMENT_IMAGES} clear photos.",
            )

        profile_count = max(0, len(self.worker_profiles))
        minimum_score = self._adaptive_profile_score_threshold(profile, profile_count)
        support_threshold = self._adaptive_support_threshold(profile, profile_count)
        required_support_hits = 2
        if profile_count <= 1:
            if profile is not None:
                required_support_hits = max(
                    2,
                    min(profile.sample_count, SINGLE_PROFILE_REQUIRED_SUPPORT_HITS),
                )

        if best_score < minimum_score:
            return (
                False,
                f"Rejected: best similarity score {best_score:.3f} is below the verification threshold {minimum_score:.3f}.",
            )

        if profile is None:
            return (
                False,
                "Rejected: no enrolled descriptor profile is available for this employee. Re-enroll the employee.",
            )

        normalized_query = query_embedding.astype(np.float32)
        query_norm = np.linalg.norm(normalized_query)
        if query_norm == 0:
            return False, "Rejected: descriptor could not be normalized."
        normalized_query = normalized_query / query_norm

        centroid_score = float(normalized_query @ profile.centroid)
        if profile_count <= 1:
            centroid_threshold = min(MAX_PROFILE_CENTROID_THRESHOLD, SINGLE_PROFILE_MIN_CENTROID_SCORE)
        else:
            centroid_threshold = min(
                MAX_PROFILE_CENTROID_THRESHOLD,
                max(
                    OPEN_SET_MIN_CENTROID_SCORE,
                    min(0.86, profile.mean_similarity - OPEN_SET_CENTROID_MARGIN),
                ),
            )
        if centroid_score < centroid_threshold:
            return (
                False,
                f"Rejected: face is not close enough to the enrolled profile ({centroid_score:.3f} < {centroid_threshold:.3f}).",
            )

        sorted_support = sorted((float(score) for score in support_scores), reverse=True)
        strong_support_hits = sum(1 for score in sorted_support if score >= support_threshold)
        if strong_support_hits < required_support_hits:
            return (
                False,
                f"Rejected: fewer than {required_support_hits} enrolled samples matched strongly enough for a safe verification.",
            )

        return True, ""

    def rebuild_index(self) -> IndexStats:
        if self.embedder.name == "lbph":
            return self._rebuild_lbph_model()

        raw_embeddings = repository.fetch_embeddings(
            backend=self.embedder.name,
            dimension=self.embedder.vector_size,
        )
        worker_ids = [worker_id for worker_id, _vector in raw_embeddings]
        vectors = [vector.astype(np.float32) for _worker_id, vector in raw_embeddings]

        self.index.build(worker_ids, vectors)
        self.worker_profiles = self._build_worker_profiles(raw_embeddings)
        self.index.save(namespace=self._index_namespace())
        return IndexStats(
            indexed_workers=repository.worker_count(),
            indexed_embeddings=self.index.size,
        )

    def _rebuild_lbph_model(self) -> IndexStats:
        training_rows = repository.fetch_training_samples(
            backend="lbph",
            dimension=self.embedder.vector_size,
        )
        if not training_rows:
            legacy_embeddings = repository.fetch_embeddings(
                backend="classical",
                dimension=self.embedder.vector_size,
            )
            if legacy_embeddings:
                self.lbph_recognizer = None
                self.lbph_label_to_worker_id = {}
                self.index.build(
                    [worker_id for worker_id, _vector in legacy_embeddings],
                    [vector.astype(np.float32) for _worker_id, vector in legacy_embeddings],
                )
                self.worker_profiles = self._build_worker_profiles(legacy_embeddings)
                self.index.save(namespace=self._index_namespace())
                return IndexStats(
                    indexed_workers=repository.worker_count(),
                    indexed_embeddings=self.index.size,
                )

            self.lbph_recognizer = None
            self.lbph_label_to_worker_id = {}
            self.worker_profiles = {}
            self.index.build([], [])
            self.index.save(namespace=self._index_namespace())
            return IndexStats(
                indexed_workers=repository.worker_count(),
                indexed_embeddings=0,
            )

        grouped: dict[int, list[np.ndarray]] = defaultdict(list)
        descriptor_rows: list[tuple[int, np.ndarray]] = []
        for worker_id, vector, image_bytes in training_rows:
            descriptor_rows.append((worker_id, vector.astype(np.float32)))
            if image_bytes is None:
                continue
            image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            grouped[worker_id].append(image)

        if not grouped:
            self.lbph_recognizer = None
            self.lbph_label_to_worker_id = {}
            self.index.build(
                [worker_id for worker_id, _vector in descriptor_rows],
                [vector.astype(np.float32) for _worker_id, vector in descriptor_rows],
            )
            self.worker_profiles = self._build_worker_profiles(descriptor_rows)
            self.index.save(namespace=self._index_namespace())
            return IndexStats(
                indexed_workers=repository.worker_count(),
                indexed_embeddings=self.index.size,
            )

        label_to_worker_id: dict[int, int] = {}
        face_images: list[np.ndarray] = []
        labels: list[int] = []
        for label, worker_id in enumerate(sorted(grouped)):
            label_to_worker_id[label] = worker_id
            for image in grouped[worker_id]:
                face_images.append(image)
                labels.append(label)

        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(face_images, np.asarray(labels, dtype=np.int32))
        self.lbph_recognizer = recognizer
        self.lbph_label_to_worker_id = label_to_worker_id
        self.index.build(
            [worker_id for worker_id, _vector in descriptor_rows],
            [vector.astype(np.float32) for _worker_id, vector in descriptor_rows],
        )
        self.worker_profiles = self._build_worker_profiles(descriptor_rows)
        self.index.save(namespace=self._index_namespace())
        return IndexStats(
            indexed_workers=repository.worker_count(),
            indexed_embeddings=self.index.size,
        )

    def list_workers(self) -> list[WorkerRead]:
        return [WorkerRead(**dict(row)) for row in repository.list_workers()]

    def list_attendance(self, limit: int = DEFAULT_LIST_LIMIT) -> list[AttendanceRow]:
        rows = []
        for row in repository.list_attendance(limit=limit):
            data = dict(row)
            # Convert here rather than in the browser: the calibration lives on
            # the server, so every surface reports the same %BAC and score.
            data["alcohol_bac_percent"] = alcohol_bac_percent(float(data.get("alcohol_ppb") or 0.0))
            data["cannabis_confidence"] = cannabis_confidence_score(float(data.get("cannabis_ratio") or 0.0))
            rows.append(AttendanceRow(**data))
        return rows

    def delete_attendance(self, attendance_id: int) -> DeleteAttendanceResult:
        attendance = repository.delete_attendance_event(attendance_id)
        if attendance is None:
            raise RuntimeError(f"No attendance record found for id '{attendance_id}'.")
        return DeleteAttendanceResult(
            id=attendance["id"],
            worker_id=attendance["worker_id"],
            employee_code=attendance["employee_code"],
            name=attendance["name"],
            deleted=True,
        )

    def architecture_note(self) -> ArchitectureNote:
        warnings = list(self.warnings)
        if self.active_embedder != self.requested_embedder:
            warnings.append(
                "The requested production embedder is not active. Install the missing backend or disable fallback to fail fast."
            )
        missing_active_embeddings = repository.embedding_count(
            self.embedder.name,
            self.embedder.vector_size,
        ) == 0
        has_lbph_face_samples = self.embedder.name == "lbph" and bool(repository.fetch_face_samples(backend="lbph"))
        if repository.worker_count() > 0 and missing_active_embeddings and not has_lbph_face_samples:
            warnings.append(
                f"No embeddings are stored for the active '{self.embedder.name}' backend yet. Re-enroll workers after switching backends."
            )
        if any(profile.sample_count < MIN_ENROLLMENT_IMAGES for profile in self.worker_profiles.values()):
            warnings.append(
                f"Recognition is blocked for users with fewer than {MIN_ENROLLMENT_IMAGES} enrolled face images. Re-enroll each employee with at least {MIN_ENROLLMENT_IMAGES} clear samples."
            )
        if self.embedder.name == "lbph" and self.lbph_recognizer is None and self.index.size > 0:
            warnings.append(
                "Recognition is using legacy descriptor fallback data. Re-enroll employees with at least 3 clear photos to restore the stronger LBPH matcher."
            )
        if self.breath_analyzer.name == "mock":
            warnings.append(
                "Breath analyzer readings are running in mock mode. Replace the mock reader when the live alcohol/cannabis sensor interface is ready."
            )
        return ArchitectureNote(
            detector=detector_backend_name(),
            requested_embedder=self.requested_embedder,
            active_embedder=self.active_embedder,
            requested_index=self.requested_index,
            active_index=self.active_index,
            fallback_enabled=ALLOW_BACKEND_FALLBACK,
            warnings=warnings,
        )

    def status(self) -> ServiceStatus:
        counts = repository.system_counts(
            backend=self.embedder.name,
            dimension=self.embedder.vector_size,
        )
        warnings = list(self.architecture_note().warnings)
        return ServiceStatus(
            indexed_workers=counts["workers"],
            indexed_embeddings=self.index.size,
            attendance_events=counts["attendance_events"],
            cache_entries=self.recognition_cache.active_entries(),
            active_detector=detector_backend_name(),
            requested_breath_analyzer=BREATH_ANALYZER_MODE,
            active_breath_analyzer=self.breath_analyzer.name,
            requested_embedder=self.requested_embedder,
            active_embedder=self.active_embedder,
            requested_index=self.requested_index,
            active_index=self.active_index,
            fallback_enabled=ALLOW_BACKEND_FALLBACK,
            warnings=warnings,
        )

    def enroll_worker(self, employee_code: str, name: str, image_bytes_list: list[bytes], replace_existing: bool = True) -> EnrollmentResult:
        if len(image_bytes_list) < MIN_ENROLLMENT_IMAGES:
            raise RuntimeError(f"Enrollment requires at least {MIN_ENROLLMENT_IMAGES} face images.")

        # Validate and embed every photo BEFORE touching the database so a bad
        # photo cannot leave the worker half-enrolled (old embeddings deleted,
        # only some new ones stored).
        prepared_samples: list[tuple[np.ndarray, Optional[bytes]]] = []
        for photo_index, image_bytes in enumerate(image_bytes_list, start=1):
            try:
                image = _decode_image(image_bytes)
                face = self._extract_enrollment_face(image)
                embedding = self.embedder.embed(face)
                encoded_face = _encode_training_face(
                    self._prepare_lbph_face(face) if self.embedder.name == "lbph" else None
                )
            except RuntimeError as exc:
                raise RuntimeError(f"Photo {photo_index} of {len(image_bytes_list)}: {exc}") from exc
            prepared_samples.append((embedding, encoded_face))

        worker = repository.upsert_worker(employee_code=employee_code, name=name)
        if replace_existing:
            repository.delete_embeddings_for_worker(worker["id"])
        added = 0
        for embedding, encoded_face in prepared_samples:
            repository.store_embedding(
                worker["id"],
                embedding,
                backend=self.embedder.name,
                face_image=encoded_face,
            )
            added += 1

        stats = self.rebuild_index()
        return EnrollmentResult(
            worker=WorkerRead(**dict(worker)),
            embeddings_added=added,
            index_size=stats.indexed_embeddings,
        )

    def _extract_enrollment_face(self, image: np.ndarray) -> np.ndarray:
        faces = detect_faces(image)
        if not faces:
            raise RuntimeError("No face detected in enrollment image.")
        if len(faces) != 1:
            raise RuntimeError("Each enrollment photo must contain exactly one face.")

        x, y, w, h = faces[0]
        crop_x, crop_y, crop_w, crop_h = expand_face_box(
            x,
            y,
            w,
            h,
            image.shape[1],
            image.shape[0],
            padding_ratio=0.18,
        )
        face = image[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
        blur_variance, brightness = self._face_quality_metrics(face)
        if not self._is_face_usable(
            face,
            w,
            h,
            blur_variance=blur_variance,
            brightness=brightness,
        ):
            raise RuntimeError(
                "Enrollment photo rejected because "
                + self._face_rejection_reason(
                    face_width=w,
                    face_height=h,
                    blur_variance=blur_variance,
                ).removeprefix("Rejected: ").rstrip(".").lower()
                + "."
            )
        return face

    def detect(self, image_bytes: bytes) -> DetectionResult:
        image = _decode_image(image_bytes)
        faces = detect_faces(image)
        return DetectionResult(
            detected_faces=len(faces),
            boxes=[DetectionBox(x=x, y=y, width=w, height=h) for (x, y, w, h) in faces],
            detector_backend=detector_backend_name(),
        )

    def _prune_breath_sessions_locked(self) -> None:
        stale_session_ids = [
            session_id
            for session_id, session in self.breath_sessions.items()
            if session.completed or (session.canceled and session.future.done())
        ]
        for session_id in stale_session_ids:
            del self.breath_sessions[session_id]

    def _active_breath_session_locked(self) -> Optional[PendingBreathSession]:
        for session in self.breath_sessions.values():
            if not session.future.done():
                return session
            if not session.completed and not session.canceled:
                return session
        return None

    def _breath_test_result_from_event(
        self,
        worker: Any,
        matched_score: float,
        event: Any,
    ) -> BreathTestResult:
        return BreathTestResult(
            worker_id=int(worker["id"]),
            employee_code=worker["employee_code"],
            name=worker["name"],
            matched_score=matched_score,
            raw_sensor_value=None if event["raw_sensor_value"] is None else float(event["raw_sensor_value"]),
            alcohol_ppb=float(event["alcohol_ppb"]),
            cannabis_ppb=float(event["cannabis_ppb"]),
            alcohol_clear=bool(event["alcohol_clear"]),
            cannabis_clear=bool(event["cannabis_clear"]),
            overall_clear=bool(event["alcohol_clear"]) and bool(event["cannabis_clear"]),
            attendance_marked=bool(event["attendance_marked"]),
            created_at=event["created_at"],
            # Converted once here so every surface -- screen, API, history --
            # shows the same numbers.
            alcohol_bac_percent=alcohol_bac_percent(float(event["alcohol_ppb"])),
            cannabis_confidence=cannabis_confidence_score(_event_float(event, "cannabis_ratio")),
            cannabis_ratio=_event_float(event, "cannabis_ratio"),
            cannabis_upper=_event_float(event, "cannabis_upper"),
            cannabis_lower=_event_float(event, "cannabis_lower"),
            alcohol_baseline=_event_float(event, "alcohol_baseline"),
            alcohol_peak=_event_float(event, "alcohol_peak"),
            cannabis_baseline=_event_float(event, "cannabis_baseline"),
            cannabis_peak=_event_float(event, "cannabis_peak"),
        )

    def start_breath_test(self, worker_id: int, camera_id: str) -> BreathTestSessionStartResult:
        worker = repository.fetch_worker(worker_id)
        if worker is None:
            raise RuntimeError(f"No worker found for id '{worker_id}'.")

        with self.breath_session_lock:
            self._prune_breath_sessions_locked()
            active_session = self._active_breath_session_locked()
            if active_session is not None:
                raise RuntimeError("A breath test is already active on this device. Finish or cancel it before starting another.")

            session_id = uuid4().hex
            started_at = datetime.utcnow()
            future = self.breath_executor.submit(
                self.breath_analyzer.read,
                worker_id=worker_id,
                camera_id=camera_id,
            )
            blow_delay_seconds = max(0.0, float(getattr(self.breath_analyzer, "blow_delay_seconds", 0.0)))
            cycle_seconds = max(0.0, float(getattr(self.breath_analyzer, "cycle_seconds", 0.0)))
            session = PendingBreathSession(
                session_id=session_id,
                worker_id=worker_id,
                camera_id=camera_id,
                started_at=started_at,
                sample_seconds=max(0.0, float(getattr(self.breath_analyzer, "sample_seconds", 0.0))),
                future=future,
                cycle_seconds=cycle_seconds,
            )
            self.breath_sessions[session_id] = session

        return BreathTestSessionStartResult(
            session_id=session_id,
            worker_id=worker_id,
            camera_id=camera_id,
            sample_seconds=max(1.0, session.sample_seconds),
            started_at=started_at,
            blow_delay_seconds=blow_delay_seconds,
            cycle_seconds=cycle_seconds,
        )

    def complete_breath_test(self, session_id: str, matched_score: float) -> BreathTestResult:
        with self.breath_session_lock:
            session = self.breath_sessions.get(session_id)
        if session is None:
            raise RuntimeError("The requested breath test session was not found.")
        if session.canceled:
            raise RuntimeError("The breath test session was canceled before completion.")

        worker = repository.fetch_worker(session.worker_id)
        if worker is None:
            raise RuntimeError(f"No worker found for id '{session.worker_id}'.")

        try:
            # Wait for the whole measurement cycle, not just the blow window:
            # purge + baseline run on the sensor board first, so timing out on
            # sample_seconds alone would discard a perfectly good reading.
            wait_seconds = max(session.cycle_seconds, session.sample_seconds) + 10.0
            reading = session.future.result(timeout=max(5.0, wait_seconds))
        except FutureTimeoutError as exc:
            raise RuntimeError("The breath sensor is still processing this exhale. Please wait a moment and try again.") from exc
        except Exception as exc:
            with self.breath_session_lock:
                session.completed = True
                self._prune_breath_sessions_locked()
            raise RuntimeError(str(exc)) from exc

        event = repository.record_screening_event(
            worker_id=session.worker_id,
            camera_id=session.camera_id,
            matched_score=matched_score,
            raw_sensor_value=reading.raw_sensor_value,
            alcohol_ppb=reading.alcohol_ppb,
            cannabis_ppb=reading.cannabis_ppb,
            alcohol_clear=reading.alcohol_clear,
            cannabis_clear=reading.cannabis_clear,
            cannabis_ratio=getattr(reading, "cannabis_ratio", 0.0),
            cannabis_upper=getattr(reading, "cannabis_upper", 0.0),
            cannabis_lower=getattr(reading, "cannabis_lower", 0.0),
            alcohol_baseline=getattr(reading, "alcohol_baseline", 0.0),
            alcohol_peak=getattr(reading, "alcohol_peak", 0.0),
            cannabis_baseline=getattr(reading, "cannabis_baseline", 0.0),
            cannabis_peak=getattr(reading, "cannabis_peak", 0.0),
        )
        with self.breath_session_lock:
            session.completed = True
            self._prune_breath_sessions_locked()
        return self._breath_test_result_from_event(worker=worker, matched_score=matched_score, event=event)

    def cancel_breath_test(self, session_id: str) -> BreathTestSessionCancelResult:
        with self.breath_session_lock:
            session = self.breath_sessions.get(session_id)
            if session is None:
                raise RuntimeError("The requested breath test session was not found.")

            session.canceled = True
            session.future.cancel()
            if session.future.done():
                self._prune_breath_sessions_locked()

        return BreathTestSessionCancelResult(session_id=session_id, canceled=True)

    def screen_worker(self, worker_id: int, camera_id: str, matched_score: float) -> BreathTestResult:
        worker = repository.fetch_worker(worker_id)
        if worker is None:
            raise RuntimeError(f"No worker found for id '{worker_id}'.")

        reading = self.breath_analyzer.read(worker_id=worker_id, camera_id=camera_id)
        event = repository.record_screening_event(
            worker_id=worker_id,
            camera_id=camera_id,
            matched_score=matched_score,
            raw_sensor_value=reading.raw_sensor_value,
            alcohol_ppb=reading.alcohol_ppb,
            cannabis_ppb=reading.cannabis_ppb,
            alcohol_clear=reading.alcohol_clear,
            cannabis_clear=reading.cannabis_clear,
            cannabis_ratio=getattr(reading, "cannabis_ratio", 0.0),
            cannabis_upper=getattr(reading, "cannabis_upper", 0.0),
            cannabis_lower=getattr(reading, "cannabis_lower", 0.0),
            alcohol_baseline=getattr(reading, "alcohol_baseline", 0.0),
            alcohol_peak=getattr(reading, "alcohol_peak", 0.0),
            cannabis_baseline=getattr(reading, "cannabis_baseline", 0.0),
            cannabis_peak=getattr(reading, "cannabis_peak", 0.0),
        )
        return self._breath_test_result_from_event(worker=worker, matched_score=matched_score, event=event)

    def delete_worker(self, employee_code: str) -> DeleteWorkerResult:
        worker = repository.delete_worker_by_employee_code(employee_code)
        if worker is None:
            raise RuntimeError(f"No worker found for employee code '{employee_code}'.")

        stats = self.rebuild_index()
        return DeleteWorkerResult(
            worker_id=worker["id"],
            employee_code=worker["employee_code"],
            name=worker["name"],
            deleted=True,
            index_size=stats.indexed_embeddings,
        )

    def recognize(self, image_bytes: bytes, camera_id: str, top_k: int = 3) -> RecognitionResult:
        image = _decode_image(image_bytes)
        if self.embedder.name == "lbph":
            return self._recognize_lbph(image=image, camera_id=camera_id, top_k=top_k)
        return self._recognize_with_descriptor_index(image=image, camera_id=camera_id, top_k=top_k)

    def _recognize_with_descriptor_index(
        self,
        image: np.ndarray,
        camera_id: str,
        top_k: int = 3,
    ) -> RecognitionResult:
        faces = detect_faces(image)
        if not faces:
            return RecognitionResult(matches=[], unknown_faces=0, detected_faces=0, boxes=[], debug_faces=[])
        if len(faces) > 1:
            return self._multiple_face_result(faces)

        debug_faces: list[FaceDebug] = []
        prepared_faces: list[tuple[FaceDebug, DescriptorConsensus]] = []
        largest_first = sorted(faces, key=lambda rect: rect[2] * rect[3], reverse=True)[:MAX_FACES_PER_REQUEST]
        for face_index, (x, y, w, h) in enumerate(largest_first):
            crop_x, crop_y, crop_w, crop_h = expand_face_box(
                x, y, w, h, image.shape[1], image.shape[0], padding_ratio=0.18
            )
            face_crop = image[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
            blur_variance, brightness = self._face_quality_metrics(face_crop)
            if not self._is_face_usable(face_crop, w, h, blur_variance=blur_variance, brightness=brightness):
                debug_faces.append(
                    FaceDebug(
                        face_index=face_index,
                        accepted=False,
                        reason=self._face_rejection_reason(
                            face_width=w,
                            face_height=h,
                            blur_variance=blur_variance,
                        ),
                        blur_variance=blur_variance,
                        brightness=brightness,
                    )
                )
                continue
            consensus = self._descriptor_consensus(face_crop, top_k=min(MAX_TOP_K, max(top_k, 5)))
            debug_entry = FaceDebug(
                face_index=face_index,
                accepted=False,
                reason="Pending descriptor search.",
                blur_variance=blur_variance,
                brightness=brightness,
                candidates=consensus.candidates,
            )
            debug_faces.append(debug_entry)
            prepared_faces.append((debug_entry, consensus))

        unknown_faces = sum(1 for face in debug_faces if not face.accepted and "Rejected" in face.reason)
        grouped_hits: dict[int, float] = defaultdict(float)

        for debug_entry, consensus in prepared_faces:
            debug_entry.candidates = consensus.candidates
            if consensus.worker_id is None or consensus.representative_embedding is None:
                unknown_faces += 1
                debug_entry.reason = consensus.rejection_reason or "Rejected: descriptor verification failed."
                continue

            best_worker_id = consensus.worker_id
            best_score = consensus.best_score
            second_best_score = consensus.second_score
            if best_score < max(MATCH_THRESHOLD, 0.55):
                unknown_faces += 1
                debug_entry.reason = "Rejected: top similarity score is below threshold."
                continue

            if (best_score - second_best_score) < max(AMBIGUITY_MARGIN, 0.03):
                unknown_faces += 1
                debug_entry.reason = "Rejected: top candidates are too close to each other."
                continue

            passed_gate, gate_reason = self._passes_open_set_gate(
                worker_id=best_worker_id,
                best_score=best_score,
                support_scores=consensus.support_scores,
                query_embedding=consensus.representative_embedding,
            )
            if not passed_gate:
                unknown_faces += 1
                debug_entry.reason = gate_reason
                continue

            confidence_score = self._calibrated_match_confidence(
                worker_id=best_worker_id,
                raw_score=best_score,
                support_scores=consensus.support_scores,
                query_embedding=consensus.representative_embedding,
            )
            confirmed, confirmed_score, confirmation_reason = self._confirm_match_candidate(
                camera_id=camera_id,
                worker_id=best_worker_id,
                score=confidence_score,
            )
            if not confirmed:
                unknown_faces += 1
                debug_entry.reason = confirmation_reason or "Pending: waiting for the same identity across more frames."
                continue

            previous_score = grouped_hits[best_worker_id]
            grouped_hits[best_worker_id] = max(previous_score, confirmed_score)
            debug_entry.accepted = True
            debug_entry.reason = "Accepted: descriptor match passed threshold and stability checks."

        matches: list[MatchResult] = []
        for worker_id, score in sorted(grouped_hits.items(), key=lambda item: item[1], reverse=True):
            worker = repository.fetch_worker(worker_id)
            if worker is None:
                continue
            matches.append(
                MatchResult(
                    worker_id=worker_id,
                    employee_code=worker["employee_code"],
                    name=worker["name"],
                    score=score,
                    attendance_marked=False,
                    source="fresh",
                )
            )

        return RecognitionResult(
            matches=matches,
            unknown_faces=unknown_faces,
            detected_faces=len(largest_first),
            boxes=[DetectionBox(x=x, y=y, width=w, height=h) for (x, y, w, h) in largest_first],
            debug_faces=debug_faces,
        )

    def _recognize_lbph(self, image: np.ndarray, camera_id: str, top_k: int = 3) -> RecognitionResult:
        faces = detect_faces(image)
        if not faces:
            return RecognitionResult(matches=[], unknown_faces=0, detected_faces=0, boxes=[], debug_faces=[])
        if len(faces) > 1:
            return self._multiple_face_result(faces)

        if self.lbph_recognizer is None:
            self._rebuild_lbph_model()
        if self.lbph_recognizer is None:
            if self.index.size > 0 and self.worker_profiles:
                return self._recognize_with_descriptor_index(
                    image=image,
                    camera_id=camera_id,
                    top_k=top_k,
                )

            return RecognitionResult(
                matches=[],
                unknown_faces=len(faces),
                detected_faces=len(faces),
                boxes=[DetectionBox(x=x, y=y, width=w, height=h) for (x, y, w, h) in faces],
                debug_faces=[
                    FaceDebug(
                        face_index=face_index,
                        accepted=False,
                        reason=(
                            "No enrolled face samples are available for recognition. "
                            f"Re-enroll the employee with at least {MIN_ENROLLMENT_IMAGES} clear photos."
                        ),
                    )
                    for face_index, _face in enumerate(faces)
                ],
            )

        unknown_faces = 0
        grouped_hits: dict[int, float] = defaultdict(float)
        debug_faces: list[FaceDebug] = []
        largest_first = sorted(faces, key=lambda rect: rect[2] * rect[3], reverse=True)[:MAX_FACES_PER_REQUEST]

        for face_index, (x, y, w, h) in enumerate(largest_first):
            crop_x, crop_y, crop_w, crop_h = expand_face_box(
                x, y, w, h, image.shape[1], image.shape[0], padding_ratio=0.18
            )
            face_crop = image[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
            blur_variance, brightness = self._face_quality_metrics(face_crop)
            if not self._is_face_usable(face_crop, w, h, blur_variance=blur_variance, brightness=brightness):
                unknown_faces += 1
                debug_faces.append(
                    FaceDebug(
                        face_index=face_index,
                        accepted=False,
                        reason=self._face_rejection_reason(
                            face_width=w,
                            face_height=h,
                            blur_variance=blur_variance,
                        ),
                        blur_variance=blur_variance,
                        brightness=brightness,
                    )
                )
                continue
            normalized_face, eyes_detected = self._prepare_lbph_face_with_eyes(face_crop)
            descriptor_consensus = self._descriptor_consensus(face_crop, top_k=min(MAX_TOP_K, 5))
            predicted_label, raw_confidence = self.lbph_recognizer.predict(normalized_face)
            worker_id = self.lbph_label_to_worker_id.get(int(predicted_label))
            descriptor_worker_id = descriptor_consensus.worker_id
            descriptor_score = descriptor_consensus.best_score
            second_descriptor_score = descriptor_consensus.second_score
            lbph_score = self._lbph_score(raw_confidence)
            debug_entry = FaceDebug(
                face_index=face_index,
                accepted=False,
                reason="Pending LBPH and descriptor checks.",
                blur_variance=blur_variance,
                brightness=brightness,
                eyes_detected=eyes_detected,
                candidates=descriptor_consensus.candidates,
            )
            if descriptor_consensus.worker_id is None or descriptor_consensus.representative_embedding is None:
                unknown_faces += 1
                debug_entry.reason = descriptor_consensus.rejection_reason or "Rejected: descriptor verification failed."
                debug_faces.append(debug_entry)
                continue
            candidate_scores: dict[int, float] = {}
            if worker_id is not None:
                candidate_scores[worker_id] = max(candidate_scores.get(worker_id, 0.0), lbph_score)
            candidate_scores[descriptor_worker_id] = max(
                candidate_scores.get(descriptor_worker_id, 0.0),
                descriptor_score,
            )

            if worker_id is not None and descriptor_worker_id == worker_id:
                candidate_scores[worker_id] = min(
                    1.0,
                    max(candidate_scores[worker_id], (lbph_score * 0.55) + (descriptor_score * 0.45)),
                )

            if not candidate_scores:
                unknown_faces += 1
                debug_entry.reason = "Rejected: no candidate survived LBPH and descriptor scoring."
                debug_faces.append(debug_entry)
                continue

            ranked_candidates = sorted(candidate_scores.items(), key=lambda item: item[1], reverse=True)
            best_worker_id, best_score = ranked_candidates[0]
            second_best_score = ranked_candidates[1][1] if len(ranked_candidates) > 1 else 0.0
            lbph_confidence_threshold = LBPH_CONFIDENCE_THRESHOLD
            if len(self.worker_profiles) <= 1:
                lbph_confidence_threshold = min(
                    lbph_confidence_threshold,
                    SINGLE_PROFILE_LBPH_CONFIDENCE_THRESHOLD,
                )
            raw_confidence_allows = worker_id is not None and raw_confidence <= lbph_confidence_threshold
            descriptor_is_clear = (descriptor_score - second_descriptor_score) >= 0.04

            if best_score < 0.6 and not raw_confidence_allows:
                unknown_faces += 1
                debug_entry.reason = "Rejected: combined score is below threshold."
                debug_faces.append(debug_entry)
                continue

            if worker_id is not None and worker_id != descriptor_worker_id:
                unknown_faces += 1
                debug_entry.reason = "Rejected: LBPH and descriptor disagree on identity."
                debug_faces.append(debug_entry)
                continue

            if (best_score - second_best_score) < 0.05 and not (raw_confidence_allows and descriptor_is_clear):
                unknown_faces += 1
                debug_entry.reason = "Rejected: best candidate is not clearly ahead of the next one."
                debug_faces.append(debug_entry)
                continue

            passed_gate, gate_reason = self._passes_open_set_gate(
                worker_id=best_worker_id,
                best_score=best_score,
                support_scores=descriptor_consensus.support_scores,
                query_embedding=descriptor_consensus.representative_embedding,
            )
            if not passed_gate:
                unknown_faces += 1
                debug_entry.reason = gate_reason
                debug_faces.append(debug_entry)
                continue

            descriptor_confidence = self._calibrated_match_confidence(
                worker_id=best_worker_id,
                raw_score=descriptor_score,
                support_scores=descriptor_consensus.support_scores,
                query_embedding=descriptor_consensus.representative_embedding,
                auxiliary_score=lbph_score if best_worker_id == worker_id else None,
            )
            accepted_score = max(descriptor_confidence, lbph_score if best_worker_id == worker_id else 0.0)
            confirmed, confirmed_score, confirmation_reason = self._confirm_match_candidate(
                camera_id=camera_id,
                worker_id=best_worker_id,
                score=accepted_score,
            )
            if not confirmed:
                unknown_faces += 1
                debug_entry.reason = confirmation_reason or "Pending: waiting for the same identity across more frames."
                debug_faces.append(debug_entry)
                continue

            previous_score = grouped_hits[best_worker_id]
            grouped_hits[best_worker_id] = max(previous_score, confirmed_score)
            debug_entry.accepted = True
            debug_entry.reason = "Accepted: LBPH and descriptor checks passed."
            debug_faces.append(debug_entry)

        matches: list[MatchResult] = []
        for worker_id, score in sorted(grouped_hits.items(), key=lambda item: item[1], reverse=True):
            worker = repository.fetch_worker(worker_id)
            if worker is None:
                continue
            matches.append(
                MatchResult(
                    worker_id=worker_id,
                    employee_code=worker["employee_code"],
                    name=worker["name"],
                    score=score,
                    attendance_marked=False,
                    source="fresh",
                )
            )

        return RecognitionResult(
            matches=matches,
            unknown_faces=unknown_faces,
            detected_faces=len(largest_first),
            boxes=[DetectionBox(x=x, y=y, width=w, height=h) for (x, y, w, h) in largest_first],
            debug_faces=debug_faces,
        )

    def _prepare_lbph_face(self, face_image: np.ndarray) -> np.ndarray:
        aligned, _eyes_detected = align_face_by_eyes(face_image, FACE_SIZE)
        return aligned

    def _prepare_lbph_face_with_eyes(self, face_image: np.ndarray) -> tuple[np.ndarray, int]:
        # Alignment already runs the eye cascade; reuse its count instead of
        # paying for a second full cascade pass per frame.
        aligned, eyes_detected = align_face_by_eyes(face_image, FACE_SIZE)
        return aligned, eyes_detected

    def _lbph_score(self, raw_confidence: float) -> float:
        return float(max(0.0, min(1.0, 1.0 - (raw_confidence / 120.0))))

    def _is_face_usable(
        self,
        face_crop: np.ndarray,
        face_width: int,
        face_height: int,
        blur_variance: Optional[float] = None,
        brightness: Optional[float] = None,
    ) -> bool:
        if face_width < MIN_FACE_WIDTH or face_height < MIN_FACE_HEIGHT:
            return False

        if blur_variance is None or brightness is None:
            blur_variance, brightness = self._face_quality_metrics(face_crop)
        if blur_variance < MIN_FACE_BLUR_VARIANCE:
            return False
        # Brightness is intentionally not gated here: dark or bright frames
        # are accepted as-is rather than rejected.
        return True

    def _face_rejection_reason(
        self,
        face_width: int,
        face_height: int,
        blur_variance: float,
    ) -> str:
        reasons: list[str] = []
        if face_width < MIN_FACE_WIDTH or face_height < MIN_FACE_HEIGHT:
            reasons.append(
                f"face is too small ({face_width}x{face_height}, minimum {MIN_FACE_WIDTH}x{MIN_FACE_HEIGHT})"
            )
        if blur_variance < MIN_FACE_BLUR_VARIANCE:
            reasons.append(
                f"face is too blurry ({blur_variance:.1f} < {MIN_FACE_BLUR_VARIANCE:.1f})"
            )
        if not reasons:
            return "Rejected by the face quality gate."
        return "Rejected: " + "; ".join(reasons) + "."

    def _face_quality_metrics(self, face_crop: np.ndarray) -> tuple[float, float]:
        if len(face_crop.shape) == 3:
            gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = face_crop
        blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(np.mean(gray))
        return blur_variance, brightness

    def _aggregate_descriptor_hits(self, hits: list) -> tuple[Optional[int], float, float]:
        if not hits:
            return None, 0.0, 0.0

        grouped: dict[int, list[float]] = defaultdict(list)
        for hit in hits:
            grouped[hit.worker_id].append(float(hit.score))

        ranked: list[tuple[int, float]] = []
        for worker_id, scores in grouped.items():
            best = max(scores)
            support_bonus = min(0.05, 0.015 * (len(scores) - 1))
            ranked.append((worker_id, min(1.0, best + support_bonus)))

        ranked.sort(key=lambda item: item[1], reverse=True)
        best_worker_id, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        return best_worker_id, best_score, second_score

    def _debug_candidates(self, hits: list, limit: int = 3) -> list[CandidateDebug]:
        grouped: dict[int, float] = {}
        for hit in hits:
            grouped[hit.worker_id] = max(grouped.get(hit.worker_id, 0.0), float(hit.score))
        ranked = sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [CandidateDebug(worker_id=worker_id, score=score) for worker_id, score in ranked]


def _decode_image(image_bytes: bytes) -> np.ndarray:
    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("Could not decode image bytes.")
    return image


def _encode_training_face(face_image: Optional[np.ndarray]) -> Optional[bytes]:
    if face_image is None:
        return None
    ok, encoded = cv2.imencode(".png", face_image)
    if not ok:
        raise RuntimeError("Could not encode training face sample.")
    return encoded.tobytes()
