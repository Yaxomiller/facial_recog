from __future__ import annotations

from dataclasses import dataclass
import importlib
import random
import time
from typing import Any

from src.v2.config import (
    BREATH_ADC_BITS,
    BREATH_ADC_BASELINE,
    BREATH_ADC_GAIN,
    BREATH_ADC_SETTLE_SECONDS,
    BREATH_ALCOHOL_SOURCE,
    BREATH_ADC_TO_ALCOHOL_OFFSET,
    BREATH_ADC_TO_ALCOHOL_SCALE,
    BREATH_ADC_TO_CANNABIS_OFFSET,
    BREATH_ADC_TO_CANNABIS_SCALE,
    BREATH_ADC_VREF,
    BREATH_ALCOHOL_THRESHOLD_PPB,
    BREATH_ANALYZER_MODE,
    BREATH_BOARD_ENABLE_GPIO,
    BREATH_CANNABIS_THRESHOLD_PPB,
    BREATH_MOCK_ALCOHOL_PPB,
    BREATH_MOCK_CANNABIS_PPB,
    BREATH_PID_SETTLE_SECONDS,
    BREATH_PLACEHOLDER_ALCOHOL_MAX_PPB,
    BREATH_PLACEHOLDER_ALCOHOL_MIN_PPB,
    BREATH_POWER_SETTLE_SECONDS,
    BREATH_SAMPLE_AGGREGATION,
    BREATH_SAMPLE_INTERVAL_SECONDS,
    BREATH_SAMPLE_SECONDS,
    BREATH_SPI_DEVICE,
    BREATH_SPI_MODE,
    BREATH_SPI_SPEED_HZ,
)


CMD_NOP = 0x00
CMD_PID_STARTUP = 0x10
CMD_PID_SHUTDOWN = 0x11
CMD_ADC_INIT = 0x20
CMD_ADC_STOP = 0x21
CMD_READ_ADC = 0x30


@dataclass(frozen=True)
class BreathReading:
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    raw_sensor_value: float | None = None


@dataclass(frozen=True)
class LiveSpiSettings:
    spi_device: str = BREATH_SPI_DEVICE
    spi_mode: int = BREATH_SPI_MODE
    spi_speed_hz: int = BREATH_SPI_SPEED_HZ
    board_enable_gpio: int = BREATH_BOARD_ENABLE_GPIO
    power_settle_seconds: float = BREATH_POWER_SETTLE_SECONDS
    pid_settle_seconds: float = BREATH_PID_SETTLE_SECONDS
    adc_settle_seconds: float = BREATH_ADC_SETTLE_SECONDS
    sample_seconds: float = BREATH_SAMPLE_SECONDS
    sample_interval_seconds: float = BREATH_SAMPLE_INTERVAL_SECONDS
    sample_aggregation: str = BREATH_SAMPLE_AGGREGATION
    adc_bits: int = BREATH_ADC_BITS
    adc_vref: float = BREATH_ADC_VREF
    adc_gain: float = BREATH_ADC_GAIN
    adc_baseline: float = BREATH_ADC_BASELINE
    alcohol_source: str = BREATH_ALCOHOL_SOURCE
    alcohol_scale: float = BREATH_ADC_TO_ALCOHOL_SCALE
    alcohol_offset: float = BREATH_ADC_TO_ALCOHOL_OFFSET
    placeholder_alcohol_min_ppb: float = BREATH_PLACEHOLDER_ALCOHOL_MIN_PPB
    placeholder_alcohol_max_ppb: float = BREATH_PLACEHOLDER_ALCOHOL_MAX_PPB
    cannabis_scale: float = BREATH_ADC_TO_CANNABIS_SCALE
    cannabis_offset: float = BREATH_ADC_TO_CANNABIS_OFFSET


class BreathAnalyzer:
    name = "base"
    startup_warnings: tuple[str, ...] = ()
    sample_seconds = BREATH_SAMPLE_SECONDS

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        raise NotImplementedError


class MockBreathAnalyzer(BreathAnalyzer):
    name = "mock"

    def __init__(self, startup_warnings: tuple[str, ...] = ()) -> None:
        self.startup_warnings = startup_warnings
        self.sample_seconds = BREATH_SAMPLE_SECONDS

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        del worker_id, camera_id
        return build_breath_reading(
            alcohol_ppb=BREATH_MOCK_ALCOHOL_PPB,
            cannabis_ppb=BREATH_MOCK_CANNABIS_PPB,
            raw_sensor_value=None,
        )


class LiveSpiBreathAnalyzer(BreathAnalyzer):
    name = "spi"

    def __init__(self, settings: LiveSpiSettings | None = None, periphery_module: Any | None = None) -> None:
        self.settings = settings or LiveSpiSettings()
        self.sample_seconds = self.settings.sample_seconds
        self.periphery_module = periphery_module or importlib.import_module("periphery")
        self.startup_warnings = ()
        if self.settings.alcohol_source != "adc":
            self.startup_warnings = (
                "Alcohol readings are currently placeholder values. "
                "The cannabis reading is live from the SPI breath board, but the alcohol script is not wired yet.",
            )

    def _send(self, spi: Any, cmd: int) -> list[int]:
        tx = [cmd, 0x00, 0x00, 0x00]
        rx = spi.transfer(tx)
        if len(rx) < 3:
            raise RuntimeError("The SPI board returned an incomplete response.")
        return [int(value) for value in rx]

    def _read_adc(self, spi: Any) -> int:
        response = self._send(spi, CMD_READ_ADC)
        return (response[1] << 8) | response[2]

    def _aggregate_samples(self, samples: list[int]) -> float:
        if not samples:
            raise RuntimeError("The SPI board did not return any ADC samples.")

        mode = self.settings.sample_aggregation
        if mode == "peak":
            return float(max(samples))
        if mode == "last":
            return float(samples[-1])
        return float(sum(samples) / len(samples))

    def _adc_to_ppb(self, raw_adc: float, scale: float, offset: float) -> float:
        adjusted_adc = max(0.0, float(raw_adc) - self.settings.adc_baseline)
        return max(0.0, (adjusted_adc * scale) + offset)

    def _adc_to_cannabis_ppb(self, raw_adc: float) -> float:
        adc_gain = float(self.settings.adc_gain)
        if adc_gain <= 0:
            raise RuntimeError("The SPI breath analyzer gain must be greater than zero.")

        fullscale = float(2 ** max(1, int(self.settings.adc_bits)))
        converted_value = max(0.0, float(raw_adc)) * (
            float(self.settings.adc_vref) / (fullscale * adc_gain)
        )
        return max(
            0.0,
            (converted_value * float(self.settings.cannabis_scale)) + float(self.settings.cannabis_offset),
        )

    def _resolve_alcohol_ppb(self, raw_adc: float) -> float:
        if self.settings.alcohol_source == "adc":
            return self._adc_to_ppb(
                raw_adc,
                scale=self.settings.alcohol_scale,
                offset=self.settings.alcohol_offset,
            )

        low = min(self.settings.placeholder_alcohol_min_ppb, self.settings.placeholder_alcohol_max_ppb)
        high = max(self.settings.placeholder_alcohol_min_ppb, self.settings.placeholder_alcohol_max_ppb)
        return max(0.0, random.uniform(low, high))

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        del worker_id, camera_id

        gpio = None
        spi = None
        samples: list[int] = []
        try:
            gpio = self.periphery_module.GPIO(self.settings.board_enable_gpio, "out")
            spi = self.periphery_module.SPI(
                self.settings.spi_device,
                self.settings.spi_mode,
                self.settings.spi_speed_hz,
            )

            gpio.write(True)
            self._sleep(self.settings.power_settle_seconds)

            self._send(spi, CMD_PID_STARTUP)
            self._sleep(self.settings.pid_settle_seconds)

            self._send(spi, CMD_ADC_INIT)
            self._sleep(self.settings.adc_settle_seconds)

            deadline = time.monotonic() + self.settings.sample_seconds
            while True:
                samples.append(self._read_adc(spi))
                if time.monotonic() >= deadline:
                    break
                self._sleep(self.settings.sample_interval_seconds)

            aggregated_adc = self._aggregate_samples(samples)
            raw_sensor_value = aggregated_adc if self.settings.alcohol_source == "adc" else None
            return build_breath_reading(
                alcohol_ppb=self._resolve_alcohol_ppb(aggregated_adc),
                cannabis_ppb=self._adc_to_cannabis_ppb(aggregated_adc),
                raw_sensor_value=raw_sensor_value,
            )
        except Exception as exc:
            raise RuntimeError(f"Live breath analyzer read failed: {exc}") from exc
        finally:
            if spi is not None:
                try:
                    self._send(spi, CMD_ADC_STOP)
                except Exception:
                    pass
                try:
                    self._sleep(self.settings.adc_settle_seconds)
                    self._send(spi, CMD_PID_SHUTDOWN)
                except Exception:
                    pass
                try:
                    spi.close()
                except Exception:
                    pass
            if gpio is not None:
                try:
                    gpio.write(False)
                except Exception:
                    pass
                try:
                    gpio.close()
                except Exception:
                    pass


def build_breath_reading(
    alcohol_ppb: float,
    cannabis_ppb: float,
    raw_sensor_value: float | None = None,
) -> BreathReading:
    alcohol_value = max(0.0, float(alcohol_ppb))
    cannabis_value = max(0.0, float(cannabis_ppb))
    raw_value = None if raw_sensor_value is None else max(0.0, float(raw_sensor_value))
    return BreathReading(
        alcohol_ppb=alcohol_value,
        cannabis_ppb=cannabis_value,
        alcohol_clear=alcohol_value <= BREATH_ALCOHOL_THRESHOLD_PPB,
        cannabis_clear=cannabis_value <= BREATH_CANNABIS_THRESHOLD_PPB,
        raw_sensor_value=raw_value,
    )


def resolve_breath_analyzer() -> BreathAnalyzer:
    if BREATH_ANALYZER_MODE == "mock":
        return MockBreathAnalyzer()

    if BREATH_ANALYZER_MODE in {"live", "spi", "hardware"}:
        try:
            return LiveSpiBreathAnalyzer()
        except Exception as exc:
            return MockBreathAnalyzer(
                startup_warnings=(
                    "Live SPI breath analyzer could not start "
                    f"({exc}). Falling back to mock readings until the sensor board is available.",
                )
            )

    return MockBreathAnalyzer(
        startup_warnings=(
            f"Unsupported breath analyzer mode '{BREATH_ANALYZER_MODE}'. Falling back to mock readings.",
        )
    )
