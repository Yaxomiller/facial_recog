from datetime import datetime

from pydantic import BaseModel, Field


class WorkerRead(BaseModel):
    id: int
    employee_code: str
    name: str
    created_at: datetime


class EnrollmentResult(BaseModel):
    worker: WorkerRead
    embeddings_added: int
    index_size: int


class DeleteWorkerResult(BaseModel):
    worker_id: int
    employee_code: str
    name: str
    deleted: bool
    index_size: int


class MatchResult(BaseModel):
    worker_id: int
    employee_code: str
    name: str
    score: float
    attendance_marked: bool
    source: str


class CandidateDebug(BaseModel):
    worker_id: int
    score: float


class FaceDebug(BaseModel):
    face_index: int
    accepted: bool
    reason: str
    blur_variance: float | None = None
    brightness: float | None = None
    eyes_detected: int | None = None
    candidates: list[CandidateDebug] = Field(default_factory=list)


class DetectionBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class DetectionResult(BaseModel):
    detected_faces: int
    boxes: list[DetectionBox]
    detector_backend: str


class RecognitionResult(BaseModel):
    matches: list[MatchResult]
    unknown_faces: int = 0
    detected_faces: int = 0
    boxes: list[DetectionBox] = Field(default_factory=list)
    debug_faces: list[FaceDebug] = Field(default_factory=list)


class BreathTestResult(BaseModel):
    worker_id: int
    employee_code: str
    name: str
    matched_score: float
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    overall_clear: bool
    attendance_marked: bool
    created_at: datetime


class AttendanceRow(BaseModel):
    id: int
    worker_id: int
    employee_code: str
    name: str
    camera_id: str
    matched_score: float
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    attendance_marked: bool
    created_at: datetime


class DeleteAttendanceResult(BaseModel):
    id: int
    worker_id: int
    employee_code: str
    name: str
    deleted: bool


class IndexStats(BaseModel):
    indexed_workers: int
    indexed_embeddings: int


class ServiceStatus(BaseModel):
    indexed_workers: int
    indexed_embeddings: int
    attendance_events: int
    cache_entries: int
    active_detector: str
    requested_embedder: str
    active_embedder: str
    requested_index: str
    active_index: str
    fallback_enabled: bool
    warnings: list[str]


class ArchitectureNote(BaseModel):
    detector: str = Field(default="Configurable face detector backend")
    embedder: str = Field(default="Classical or LBPH face descriptor backend")
    index: str = Field(default="LSH or vector index backend")
    production_upgrade: str = Field(default="For stronger accuracy later, replace the descriptor with a supported deep embedding backend and keep the same API/index structure.")
    requested_embedder: str
    active_embedder: str
    requested_index: str
    active_index: str
    fallback_enabled: bool
    warnings: list[str] = Field(default_factory=list)
