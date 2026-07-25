from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from src.breath_analyzer import (
    CMD_AFE_STARTUP,
    CMD_PID_SHUTDOWN,
    CMD_PID_STARTUP,
    FRAME_MAX,
    MAX_RECORDS,
    PID_MV_PER_LSB,
    RECORD_SIZE,
    SRC_AD5941,
    SRC_AD7798,
    BreathReading,
    ChannelResult,
    CycleResult,
    MockBreathAnalyzer,
    SpiBreathAnalyzer,
    area_ratio,
    build_reading_from_cycle,
    crc16_ccitt,
    resolve_breath_analyzer,
    sample_mv,
)


def _build_frame(records: list[tuple[int, int, int]]) -> list[int]:
    """Build a valid 246-byte sensor frame: 0xAA 0x55, count, pad, records, CRC."""
    payload = bytearray(FRAME_MAX)
    payload[0] = 0xAA
    payload[1] = 0x55
    payload[2] = min(len(records), MAX_RECORDS)
    payload[3] = 0
    for index, (tick, source, value) in enumerate(records[:MAX_RECORDS]):
        struct.pack_into("<IH2xi", payload, 4 + index * RECORD_SIZE, tick, source, value)
    crc = crc16_ccitt(bytes(payload[:-2]))
    payload[-2] = (crc >> 8) & 0xFF
    payload[-1] = crc & 0xFF
    return list(payload)


class _FakeGPIO:
    def __init__(self, chip: str, line: int, direction: str, edge: str = "none") -> None:
        self.chip = chip
        self.line = line
        self.direction = direction
        self.edge = edge
        self.writes: list[bool] = []

    def write(self, value: bool) -> None:
        self.writes.append(bool(value))

    def read(self) -> bool:
        return False   # doorbell asserted: a frame is always available

    def poll(self, _timeout: float = 0) -> bool:
        return False

    def read_event(self) -> None:
        return None


class _FakeSPI:
    def __init__(self, device: str, mode: int, speed_hz: int) -> None:
        self.device = device
        self.mode = mode
        self.speed_hz = speed_hz
        self.commands: list[int] = []
        self.frames: list[list[int]] = []

    def transfer(self, tx: list[int]) -> list[int]:
        self.commands.append(int(tx[0]))
        if self.frames:
            return self.frames.pop(0)
        return _build_frame([])


class _FakePeriphery:
    def __init__(self) -> None:
        self.gpios: list[_FakeGPIO] = []
        self.spis: list[_FakeSPI] = []

    def GPIO(self, chip: str, line: int, direction: str, edge: str = "none") -> _FakeGPIO:  # noqa: N802
        gpio = _FakeGPIO(chip, line, direction, edge)
        self.gpios.append(gpio)
        return gpio

    def SPI(self, device: str, mode: int, speed_hz: int) -> _FakeSPI:  # noqa: N802
        spi = _FakeSPI(device, mode, speed_hz)
        self.spis.append(spi)
        return spi


def _make_spi_analyzer(periphery: _FakePeriphery) -> SpiBreathAnalyzer:
    # Skip the keepalive thread and the reset sleeps in tests.
    with patch("threading.Thread"), patch("time.sleep"):
        return SpiBreathAnalyzer(periphery_module=periphery)


class FrameProtocolTests(unittest.TestCase):
    def test_crc16_ccitt_matches_the_known_check_value(self) -> None:
        # Standard CRC-16/CCITT-FALSE check: "123456789" -> 0x29B1.
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_frame_layout_is_246_bytes(self) -> None:
        self.assertEqual(FRAME_MAX, 246)
        self.assertEqual(FRAME_MAX, 4 + MAX_RECORDS * RECORD_SIZE + 2)

    def test_exchange_decodes_records_from_a_valid_frame(self) -> None:
        analyzer = _make_spi_analyzer(_FakePeriphery())
        analyzer.spi.frames.append(
            _build_frame([(1000, SRC_AD5941, 1234), (1010, SRC_AD7798, 567)])
        )

        records, error = analyzer._exchange(0x00)

        self.assertIsNone(error)
        self.assertEqual(records, [(1000, SRC_AD5941, 1234), (1010, SRC_AD7798, 567)])

    def test_exchange_rejects_a_corrupt_crc(self) -> None:
        analyzer = _make_spi_analyzer(_FakePeriphery())
        frame = _build_frame([(1000, SRC_AD5941, 10)])
        frame[-1] ^= 0xFF   # break the CRC
        analyzer.spi.frames.append(frame)

        records, error = analyzer._exchange(0x00)

        self.assertIsNone(records)
        self.assertEqual(error, "bad CRC")

    def test_exchange_rejects_a_bad_header(self) -> None:
        analyzer = _make_spi_analyzer(_FakePeriphery())
        frame = _build_frame([])
        frame[0] = 0x00
        analyzer.spi.frames.append(frame)

        records, error = analyzer._exchange(0x00)

        self.assertIsNone(records)
        self.assertIn("bad header", error)

    def test_board_is_wired_and_reset_on_startup(self) -> None:
        analyzer = _make_spi_analyzer(_FakePeriphery())

        # BRD_ON must be requested atomically HIGH so the STM32 is not reset
        # by an intermediate LOW on every start.
        self.assertEqual(analyzer.trigger.direction, "high")
        self.assertEqual(analyzer.ready.direction, "in")
        self.assertEqual(analyzer.ready.edge, "falling")
        # Deliberate reset pulse: low then high.
        self.assertEqual(analyzer.trigger.writes, [False, True])
        # The pump is active-high and must start off.
        self.assertEqual(analyzer.pump.writes[0], False)


class UnitConversionTests(unittest.TestCase):
    def test_pid_scale_matches_the_ad7798_datasheet(self) -> None:
        self.assertAlmostEqual(PID_MV_PER_LSB, 0.019073, places=6)

    def test_sample_mv_converts_each_source_correctly(self) -> None:
        # AD5941: nA -> mV at Rtia = 4 kOhm, so 1000 nA = 4 mV.
        self.assertAlmostEqual(sample_mv(SRC_AD5941, 1000.0), 4.0, places=6)
        # AD7798: codes -> mV.
        self.assertAlmostEqual(sample_mv(SRC_AD7798, 100.0), 100 * PID_MV_PER_LSB, places=9)


class ConformityScoreTests(unittest.TestCase):
    def test_area_split_sums_to_the_total_area(self) -> None:
        # Flat 2 mV curve for 1000 ms, threshold 0.4 mV.
        samples = [(0, 0, 0, 2.0), (1000, 0, 0, 2.0)]
        areas = area_ratio(samples, 0.4)

        self.assertAlmostEqual(areas["upper"], 1.6, places=4)   # (2.0-0.4) * 1s
        self.assertAlmostEqual(areas["lower"], 0.4, places=4)   # capped at 0.4
        self.assertAlmostEqual(areas["upper"] + areas["lower"], 2.0, places=4)
        self.assertAlmostEqual(areas["ratio"], 4.0, places=3)   # 1.6 / 0.4
        self.assertEqual(areas["points"], 2)

    def test_curve_entirely_below_the_threshold_has_no_upper_area(self) -> None:
        samples = [(0, 0, 0, 0.1), (1000, 0, 0, 0.1)]
        areas = area_ratio(samples, 0.4)

        self.assertEqual(areas["upper"], 0.0)
        self.assertAlmostEqual(areas["lower"], 0.1, places=4)
        self.assertEqual(areas["ratio"], 0.0)

    def test_negative_noise_is_clamped_and_never_creates_area(self) -> None:
        samples = [(0, 0, 0, -5.0), (1000, 0, 0, -5.0)]
        areas = area_ratio(samples, 0.4)

        self.assertEqual(areas["upper"], 0.0)
        self.assertEqual(areas["lower"], 0.0)
        self.assertEqual(areas["ratio"], 0.0)

    def test_empty_trace_yields_a_zero_score_without_dividing_by_zero(self) -> None:
        areas = area_ratio([], 0.4)

        self.assertEqual(areas["ratio"], 0.0)
        self.assertEqual(areas["points"], 0)


class ReadingBuildTests(unittest.TestCase):
    def _cycle(self, alcohol_mvs: float, cannabis_mvs: float, samples=()) -> CycleResult:
        return CycleResult(
            alcohol=ChannelResult(baseline=1000.0, peak=250.0, peak_t_ms=100,
                                  integral_mvs=alcohol_mvs),
            cannabis=ChannelResult(baseline=600.0, peak=40.0, peak_t_ms=100,
                                   integral_mvs=cannabis_mvs, samples=samples),
        )

    def test_reading_carries_the_conformity_score(self) -> None:
        samples = ((0, 0, 0, 2.0), (1000, 0, 0, 2.0))
        reading = build_reading_from_cycle(self._cycle(1.0, 1.0, samples))

        self.assertAlmostEqual(reading.cannabis_ratio, 4.0, places=3)
        self.assertAlmostEqual(reading.cannabis_upper, 1.6, places=4)
        self.assertAlmostEqual(reading.cannabis_lower, 0.4, places=4)
        self.assertEqual(reading.cannabis_points, 2)

    def test_readings_under_the_limits_are_clear(self) -> None:
        reading = build_reading_from_cycle(self._cycle(1.0, 1.0))

        self.assertTrue(reading.alcohol_clear)
        self.assertTrue(reading.cannabis_clear)

    def test_readings_over_the_limits_are_flagged(self) -> None:
        # Defaults: alcohol limit 15 mV*s, cannabis limit 3 mV*s.
        reading = build_reading_from_cycle(self._cycle(20.0, 5.0))

        self.assertFalse(reading.alcohol_clear)
        self.assertFalse(reading.cannabis_clear)

    def test_baselines_and_peaks_are_converted_to_display_units(self) -> None:
        reading = build_reading_from_cycle(self._cycle(1.0, 1.0))

        self.assertAlmostEqual(reading.alcohol_baseline, 1.0, places=3)   # nA -> uA
        self.assertAlmostEqual(reading.alcohol_peak, 0.25, places=3)
        self.assertAlmostEqual(reading.cannabis_baseline, 600.0 * PID_MV_PER_LSB, places=3)
        self.assertAlmostEqual(reading.cannabis_peak, 40.0 * PID_MV_PER_LSB, places=3)


class MockAnalyzerTests(unittest.TestCase):
    def test_mock_cycle_produces_a_trace_and_a_conformity_score(self) -> None:
        analyzer = MockBreathAnalyzer()
        with patch("src.breath_analyzer.BREATH_PURGE_SECONDS", 0.0):
            with patch("src.breath_analyzer.BREATH_BASELINE_SECONDS", 0.0):
                cycle = analyzer.run_cycle(measure_seconds=1.0)

        self.assertGreater(len(cycle.cannabis.samples), 5)
        reading = build_reading_from_cycle(cycle)
        self.assertIsInstance(reading, BreathReading)
        self.assertGreaterEqual(reading.cannabis_ratio, 0.0)
        self.assertEqual(reading.cannabis_points, len(cycle.cannabis.samples))

    def test_cycle_time_includes_purge_and_baseline(self) -> None:
        analyzer = MockBreathAnalyzer()
        analyzer.sample_seconds = 10.0

        # Defaults: 15s purge + 5s baseline before the 10s blow.
        self.assertAlmostEqual(analyzer.blow_delay_seconds, 20.0, places=3)
        self.assertAlmostEqual(analyzer.cycle_seconds, 30.0, places=3)


class ResolveAnalyzerTests(unittest.TestCase):
    def test_mock_mode_returns_the_mock_analyzer(self) -> None:
        with patch("src.breath_analyzer.BREATH_ANALYZER_MODE", "mock"):
            self.assertIsInstance(resolve_breath_analyzer(), MockBreathAnalyzer)

    def test_spi_mode_falls_back_to_mock_when_the_board_is_missing(self) -> None:
        with patch("src.breath_analyzer.BREATH_ANALYZER_MODE", "spi"):
            with patch("src.breath_analyzer.SpiBreathAnalyzer", side_effect=RuntimeError("no board")):
                analyzer = resolve_breath_analyzer()

        self.assertIsInstance(analyzer, MockBreathAnalyzer)
        self.assertTrue(any("no board" in warning for warning in analyzer.startup_warnings))


class CommandConstantTests(unittest.TestCase):
    def test_command_opcodes_match_the_sensor_firmware(self) -> None:
        self.assertEqual(CMD_PID_STARTUP, 0xA0)
        self.assertEqual(CMD_PID_SHUTDOWN, 0xA1)
        self.assertEqual(CMD_AFE_STARTUP, 0xB0)


if __name__ == "__main__":
    unittest.main()
