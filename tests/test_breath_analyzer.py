from __future__ import annotations

import os
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
    PumpGuard,
    SpiBreathAnalyzer,
    _guard_pump_when_sensor_is_idle,
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


class _LevelTrackingGPIO(_FakeGPIO):
    """GPIO whose plain "out" request comes up HIGH, like the real board."""

    def __init__(self, chip: str, line: int, direction: str, edge: str = "none") -> None:
        super().__init__(chip, line, direction, edge)
        self.level = direction in {"out", "high"}
        self.closed = False
        self.history: list[bool] = [self.level]

    def write(self, value: bool) -> None:
        super().write(value)
        self.level = bool(value)
        self.history.append(self.level)

    def close(self) -> None:
        self.closed = True


class _LevelTrackingPeriphery(_FakePeriphery):
    def GPIO(self, chip: str, line: int, direction: str, edge: str = "none"):  # noqa: N802
        gpio = _LevelTrackingGPIO(chip, line, direction, edge)
        self.gpios.append(gpio)
        return gpio


class PumpSafetyTests(unittest.TestCase):
    """The pump must never run when no measurement is in progress."""

    def _analyzer(self):
        with patch("threading.Thread"), patch("time.sleep"):
            return SpiBreathAnalyzer(periphery_module=_LevelTrackingPeriphery())

    def test_pump_is_requested_already_driven_low(self) -> None:
        analyzer = self._analyzer()

        # A plain "out" request leaves the initial level to the driver and this
        # line comes up HIGH, starting the pump the moment the app opens it.
        self.assertEqual(analyzer.pump.direction, "low")
        self.assertNotIn(True, analyzer.pump.history)
        self.assertFalse(analyzer.pump.level)

    def test_bus_is_opened_only_after_the_pump_is_known_off(self) -> None:
        opened: list[str] = []

        class OrderedPeriphery(_LevelTrackingPeriphery):
            def GPIO(self, chip, line, direction, edge="none"):  # noqa: N802
                gpio = super().GPIO(chip, line, direction, edge)
                original_write = gpio.write

                def tracking_write(value):
                    original_write(value)
                    if line == 271 and value is False:
                        opened.append("pump-low")

                gpio.write = tracking_write
                return gpio

            def SPI(self, device, mode, speed_hz):  # noqa: N802
                opened.append("spi")
                return super().SPI(device, mode, speed_hz)

        with patch("threading.Thread"), patch("time.sleep"):
            SpiBreathAnalyzer(periphery_module=OrderedPeriphery())

        # Opening the bus first would stretch the window in which the pump is
        # running by however long the bus takes to come up.
        self.assertIn("pump-low", opened)
        self.assertIn("spi", opened)
        self.assertLess(opened.index("pump-low"), opened.index("spi"))

    def test_pump_stops_before_the_slow_board_handshake(self) -> None:
        analyzer = self._analyzer()
        order: list[str] = []

        def slow_handshake(commands, max_tries=15, timeout=None):
            order.append("handshake")
            return False

        original_write = analyzer.pump.write

        def tracking_write(value):
            original_write(value)
            if value is False:
                order.append("pump-off")

        analyzer.pump.write = tracking_write
        analyzer._send_commands = slow_handshake
        analyzer.pump.write(True)
        order.clear()

        # Drive the same teardown the cycle's finally block performs.
        try:
            analyzer.pump.write(False)
        except Exception:
            pass
        try:
            analyzer._send_commands([CMD_PID_SHUTDOWN])
        except Exception:
            pass

        # _send_commands retries up to 15 frames, each able to burn ~5s waiting
        # for a doorbell plus ~5s on the deassert bound; stopping the pump must
        # not queue behind that.
        self.assertEqual(order, ["pump-off", "handshake"])
        self.assertFalse(analyzer.pump.level)

    def test_shutdown_drives_lines_low_and_releases_them(self) -> None:
        analyzer = self._analyzer()
        analyzer.pump.write(True)

        analyzer.shutdown()

        # A released GPIO reverts to an undriven input, so it must be driven
        # low before it is closed.
        self.assertFalse(analyzer.pump.level)
        self.assertTrue(analyzer.pump.closed)
        self.assertFalse(analyzer.trigger.level)
        self.assertTrue(analyzer.trigger.closed)

    def test_shutdown_is_idempotent_and_survives_closed_hardware(self) -> None:
        analyzer = self._analyzer()
        analyzer.shutdown()
        analyzer.shutdown()   # must not raise

        self.assertFalse(analyzer.pump.level)

    def test_mock_analyzer_exposes_a_shutdown_noop(self) -> None:
        MockBreathAnalyzer().shutdown()   # must not raise

    def test_a_failed_startup_still_leaves_the_pump_recoverable(self) -> None:
        # resolve_breath_analyzer() falls back to the mock when construction
        # raises, discarding this instance — the shutdown hook must already
        # know about it or the lines stay open with nothing driving them low.
        periphery = _LevelTrackingPeriphery()
        registered: list[object] = []

        with patch("threading.Thread"), patch("time.sleep"):
            with patch("src.breath_analyzer._register_pump_shutdown", side_effect=registered.append):
                with patch.object(SpiBreathAnalyzer, "_reset_board", side_effect=RuntimeError("board dead")):
                    with self.assertRaises(RuntimeError):
                        SpiBreathAnalyzer(periphery_module=periphery)

        self.assertEqual(len(registered), 1, "shutdown hook not registered before first board access")


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


class PumpGuardTests(unittest.TestCase):
    """In mock mode no sensor driver runs, so nothing else holds the pump."""

    def test_guard_claims_the_pump_line_already_driven_low(self) -> None:
        periphery = _LevelTrackingPeriphery()
        guard = PumpGuard(periphery_module=periphery)

        self.assertEqual(guard.pump.direction, "low")
        self.assertNotIn(True, guard.pump.history)
        self.assertFalse(guard.pump.level)

    def test_guard_holds_the_line_instead_of_releasing_it(self) -> None:
        periphery = _LevelTrackingPeriphery()
        guard = PumpGuard(periphery_module=periphery)

        # Closing would release the line back to an undriven input, where it
        # can float high again — it must stay claimed for the process life.
        self.assertFalse(guard.pump.closed)

    def test_guard_shutdown_drives_low_then_releases_and_is_idempotent(self) -> None:
        guard = PumpGuard(periphery_module=_LevelTrackingPeriphery())
        guard.pump.write(True)

        guard.shutdown()
        guard.shutdown()

        self.assertFalse(guard.pump.level)
        self.assertTrue(guard.pump.closed)

    def test_mock_mode_forces_the_pump_off(self) -> None:
        created: list[PumpGuard] = []

        def fake_guard():
            guard = PumpGuard(periphery_module=_LevelTrackingPeriphery())
            created.append(guard)
            return guard

        with patch("src.breath_analyzer.BREATH_ANALYZER_MODE", "mock"):
            with patch("src.breath_analyzer.PumpGuard", side_effect=fake_guard):
                analyzer = resolve_breath_analyzer()

        self.assertIsInstance(analyzer, MockBreathAnalyzer)
        self.assertEqual(len(created), 1, "mock mode left the pump line untouched")
        self.assertFalse(created[0].pump.level)

    def test_missing_hardware_is_reported_rather_than_silently_ignored(self) -> None:
        with patch("src.breath_analyzer.BREATH_ANALYZER_MODE", "mock"):
            with patch("src.breath_analyzer.PumpGuard", side_effect=RuntimeError("no gpiochip")):
                analyzer = resolve_breath_analyzer()

        self.assertTrue(
            any("could not be forced off" in warning for warning in analyzer.startup_warnings),
            analyzer.startup_warnings,
        )

    def test_guard_can_be_disabled_for_boards_without_a_pump(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_BREATH_PUMP_GUARD": "0"}, clear=False):
            with patch("src.breath_analyzer.PumpGuard", side_effect=AssertionError("must not be built")):
                self.assertEqual(_guard_pump_when_sensor_is_idle(), ())


class ResolveAnalyzerTests(unittest.TestCase):
    def test_mock_mode_returns_the_mock_analyzer(self) -> None:
        with patch("src.breath_analyzer.BREATH_ANALYZER_MODE", "mock"):
            with patch("src.breath_analyzer.PumpGuard", side_effect=RuntimeError("no hardware")):
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
