from __future__ import annotations

from dataclasses import dataclass

from src.v2.config import (
    BREATH_ALCOHOL_THRESHOLD_PPB,
    BREATH_ANALYZER_MODE,
    BREATH_CANNABIS_THRESHOLD_PPB,
    BREATH_MOCK_ALCOHOL_PPB,
    BREATH_MOCK_CANNABIS_PPB,
)


@dataclass(frozen=True)
class BreathReading:
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool


class BreathAnalyzer:
    name = "base"

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        raise NotImplementedError


class MockBreathAnalyzer(BreathAnalyzer):
    name = "mock"

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        del worker_id, camera_id
        return build_breath_reading(
            alcohol_ppb=BREATH_MOCK_ALCOHOL_PPB,
            cannabis_ppb=BREATH_MOCK_CANNABIS_PPB,
        )


def build_breath_reading(alcohol_ppb: float, cannabis_ppb: float) -> BreathReading:
    alcohol_value = max(0.0, float(alcohol_ppb))
    cannabis_value = max(0.0, float(cannabis_ppb))
    return BreathReading(
        alcohol_ppb=alcohol_value,
        cannabis_ppb=cannabis_value,
        alcohol_clear=alcohol_value <= BREATH_ALCOHOL_THRESHOLD_PPB,
        cannabis_clear=cannabis_value <= BREATH_CANNABIS_THRESHOLD_PPB,
    )


def resolve_breath_analyzer() -> BreathAnalyzer:
    if BREATH_ANALYZER_MODE == "mock":
        return MockBreathAnalyzer()
    return MockBreathAnalyzer()
