from datetime import datetime
from typing import Optional

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
    blur_variance: Optional[float] = None
    brightness: Optional[float] = None
    eyes_detected: Optional[int] = None
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
    raw_sensor_value: Optional[float] = None
    # alcohol_ppb / cannabis_ppb carry the mV*s integrals of the delta above
    # the fresh-air baseline (field names kept for compatibility).
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    overall_clear: bool
    attendance_marked: bool
    created_at: datetime
    # Operator-facing values: %BAC for alcohol, 0-1 confidence for cannabis.
    alcohol_bac_percent: float = 0.0
    cannabis_confidence: float = 0.0
    # Cannabis conformity score: upper/lower area split of the exhale curve.
    cannabis_ratio: float = 0.0
    cannabis_upper: float = 0.0
    cannabis_lower: float = 0.0
    # AD5941 in uA, AD7798 in mV.
    alcohol_baseline: float = 0.0
    alcohol_peak: float = 0.0
    cannabis_baseline: float = 0.0
    cannabis_peak: float = 0.0


class BreathTestSessionStartResult(BaseModel):
    session_id: str
    worker_id: int
    camera_id: str
    sample_seconds: float          # the blow window
    started_at: datetime
    # Purge + baseline run on the sensor board BEFORE the subject blows; the
    # UI must wait this long before prompting for the exhale.
    blow_delay_seconds: float = 0.0
    cycle_seconds: float = 0.0     # total wall time of the measurement cycle


class BreathTestSessionCancelResult(BaseModel):
    session_id: str
    canceled: bool


class AttendanceRow(BaseModel):
    id: int
    worker_id: int
    employee_code: str
    name: str
    camera_id: str
    matched_score: float
    raw_sensor_value: Optional[float] = None
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    attendance_marked: bool
    created_at: datetime
    # Operator-facing values, derived the same way as on a live result so the
    # history table and the scan screen never disagree.
    alcohol_bac_percent: float = 0.0
    cannabis_confidence: float = 0.0
    cannabis_ratio: float = 0.0
    cannabis_upper: float = 0.0
    cannabis_lower: float = 0.0


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
    # "spi" when the sensor board is driving the readings, "mock" when they
    # are simulated (in which case the pump never runs).
    requested_breath_analyzer: str = ""
    active_breath_analyzer: str = ""
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
