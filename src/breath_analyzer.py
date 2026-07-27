"""Breath sensor drivers — DiDies breathalyzer measurement cycle.

Ported from the BreathCheck handheld analyzer so both products drive the same
STM32 sensor board with identical timing, framing and maths.

Two implementations behind one interface:

- MockBreathAnalyzer : simulates the same phased cycle on development machines.
- SpiBreathAnalyzer  : the STM32 sensor board over SPI (doorbell protocol).

Measurement cycle, timed in the STM32 tick domain (immune to host latency):

  PURGE     pump ON (active-high), PID + alcohol AFE switched on via frame
            commands; readings streamed, not analysed
  BASELINE  per-sensor averages -> fresh-air zero
  MEASURE   subject blows; per-sample delta = x - baseline, trapezoidal
            integral over the window for BOTH sensors, reported in mV*s
            (the AL-05P datasheet specs linearity as the INTEGRAL of output);
            peak deltas tracked too
  IDLE      PID + pump off. Alcohol AFE sampling stays on so the real board,
            whose deployed firmware has no idle SYS keepalive, continues to
            provide doorbell frames that can carry the next START command.
            A background keepalive thread answers every idle doorbell within
            the 100 ms protocol window — an unanswered stream desyncs the
            STM32 SPI slave and made the FIRST scan after idle fail with
            "no frames from sensor". If no valid frame arrives for
            BREATH_STREAM_DEAD_SECONDS the keepalive resets the board and
            restarts AFE sampling by itself, so START always lands on a live
            stream.

Sources:  1 = AD7798 (PID / cannabis, ADC codes, 19.073 uV/LSB)
          2 = AD5941 (alcohol fuel cell, nA; V = I * Rtia at Rtia = 4k)
          3 = SYS 1 Hz keepalive (val bit0 = PID on, bit1 = AFE running)

The board is powered once and left on between cycles; the firmware's
zero-offset calibration runs at its boot. BRD_ON is requested as output-HIGH
in one atomic step ("high") — requesting plain "out" would drive it LOW first
and reset the STM32 on every start.

Readings are uncalibrated INTEGRALS in mV*s. The `alcohol_ppb` /
`cannabis_ppb` field names are kept for schema and database compatibility, but
the values they carry are mV*s integrals, not parts per billion.
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass
import importlib
import logging
import math
import os
import random
import signal
import struct
import threading
import time
import weakref
from typing import Any, Callable, Optional

from src.v2.config import (
    BREATH_ALCOHOL_THRESHOLD_PPB,
    BREATH_ANALYZER_MODE,
    BREATH_BASELINE_SECONDS,
    BREATH_BOARD_BOOT_SECONDS,
    BREATH_BOARD_ENABLE_GPIO,
    BREATH_BOARD_RESET_SECONDS,
    BREATH_CANNABIS_THRESHOLD_MV,
    BREATH_CANNABIS_THRESHOLD_PPB,
    BREATH_DOORBELL_TIMEOUT_SECONDS,
    BREATH_GPIO_CHIP,
    BREATH_MOCK_ALCOHOL_MAX,
    BREATH_MOCK_ALCOHOL_MIN,
    BREATH_MOCK_CANNABIS_MAX,
    BREATH_MOCK_CANNABIS_MIN,
    BREATH_PUMP_GPIO,
    BREATH_PURGE_SECONDS,
    BREATH_READY_GPIO,
    BREATH_RTIA_KOHM,
    BREATH_SAMPLE_SECONDS,
    BREATH_SETTLE_SLOPE_NA_S,
    BREATH_SETTLE_WINDOW_MS,
    BREATH_SPI_DEVICE,
    BREATH_SPI_MODE,
    BREATH_SPI_SPEED_HZ,
    BREATH_STABILIZE_MAX_S,
    BREATH_STREAM_DEAD_SECONDS,
)

logger = logging.getLogger("attendance.breath")

CMD_NONE = 0x00
CMD_PID_STARTUP = 0xA0
CMD_PID_SHUTDOWN = 0xA1
CMD_AFE_STARTUP = 0xB0
CMD_AFE_SHUTDOWN = 0xB1

SRC_AD7798, SRC_AD5941, SRC_SYS = 1, 2, 3

MAX_RECORDS = 20
RECORD_SIZE = 12
FRAME_MAX = 4 + MAX_RECORDS * RECORD_SIZE + 2  # 246 bytes: hdr + records + CRC16

# AD5941: V = I * Rtia -> 1 uA = 4 mV at Rtia = 4 kOhm (LPTIARTIA_4K).
# AD7798: mV per LSB (unipolar, gain 2, Vref 2.5 V).
PID_MV_PER_LSB = 2.5 / (2 * 65536) * 1000.0  # 0.019073 mV

BASELINE_SPREAD_WARN = {
    SRC_AD5941: 200,  # nA
    SRC_AD7798: 300,  # codes (PID drifts during warm-up)
}

# progress(phase, elapsed_seconds, total_seconds);
# phase: starting|recovering|purge|baseline|measure
ProgressFn = Callable[[str, float, float], None]


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _finite(value: float) -> float:
    return float(value) if value == value and abs(value) != float("inf") else 0.0


@dataclass(frozen=True)
class ChannelResult:
    baseline: float        # raw units (AD5941 nA, AD7798 codes)
    peak: float            # raw delta above baseline
    peak_t_ms: int         # ms into the cycle at the peak
    integral_mvs: float    # trapezoidal integral of the delta, mV*s
    stable: bool = True    # baseline spread within tolerance
    # Exhale trace, one entry per sample in the MEASURE window:
    # (ms into the blow, raw ADC value, delta above baseline, delta in mV)
    samples: tuple[tuple[int, float, float, float], ...] = ()


@dataclass(frozen=True)
class CycleResult:
    alcohol: ChannelResult    # AD5941 fuel cell
    cannabis: ChannelResult   # AD7798 PID


@dataclass(frozen=True)
class BreathReading:
    # alcohol_ppb / cannabis_ppb carry the mV*s integrals of the delta above
    # the fresh-air baseline (names kept for schema/DB compatibility).
    alcohol_ppb: float
    cannabis_ppb: float
    alcohol_clear: bool
    cannabis_clear: bool
    raw_sensor_value: Optional[float] = None
    # Cannabis conformity score: upper/lower area split of the exhale curve.
    cannabis_ratio: float = 0.0
    cannabis_upper: float = 0.0
    cannabis_lower: float = 0.0
    cannabis_threshold_mv: float = BREATH_CANNABIS_THRESHOLD_MV
    cannabis_points: int = 0
    # AD5941 in uA, AD7798 in mV.
    alcohol_baseline: float = 0.0
    alcohol_peak: float = 0.0
    cannabis_baseline: float = 0.0
    cannabis_peak: float = 0.0
    baseline_stable: bool = True


def _mvs(src: int, integ_raw_ms: float) -> float:
    """Convert a raw trapezoidal integral (raw-unit * ms) to mV*s."""
    if src == SRC_AD5941:
        return integ_raw_ms * BREATH_RTIA_KOHM / 1e6      # nA*ms -> mV*s
    return integ_raw_ms * PID_MV_PER_LSB / 1000.0         # code*ms -> mV*s


def sample_mv(src: int, raw_delta: float) -> float:
    """Convert one raw sample delta to mV (V = I*Rtia for the fuel cell)."""
    if src == SRC_AD5941:
        return raw_delta * BREATH_RTIA_KOHM / 1000.0      # nA -> mV
    return raw_delta * PID_MV_PER_LSB                     # codes -> mV


def area_ratio(samples, threshold_mv: float) -> dict[str, float]:
    """Cannabis conformity score.

    Split the area under the exhale curve with a horizontal line at
    `threshold_mv` and return upper/lower areas (mV*s) plus their ratio.

    The trace is a positive bell. For each sample the curve height y is
    clamped at 0 (noise below the baseline is not negative area):
      upper contribution = max(0, y - threshold)   -> the cap above the line
      lower contribution = min(y, threshold)       -> the part beneath the line
    Both are integrated over time with the trapezoid rule, so
    upper + lower == the total area under the curve.
    """
    upper_ms = lower_ms = 0.0
    previous: Optional[tuple[int, float, float]] = None
    for sample in samples:
        t_ms, _adc, _delta, mv = sample
        height = max(0.0, float(mv))
        upper = max(0.0, height - threshold_mv)
        lower = min(height, threshold_mv)
        if previous is not None:
            prev_t, prev_upper, prev_lower = previous
            span = t_ms - prev_t
            upper_ms += (upper + prev_upper) / 2.0 * span
            lower_ms += (lower + prev_lower) / 2.0 * span
        previous = (t_ms, upper, lower)

    upper_mvs = upper_ms / 1000.0      # mV*ms -> mV*s
    lower_mvs = lower_ms / 1000.0
    ratio = (upper_mvs / lower_mvs) if lower_mvs > 0 else 0.0
    return {
        "upper": round(upper_mvs, 4),
        "lower": round(lower_mvs, 4),
        "ratio": round(ratio, 3),
        "threshold": threshold_mv,
        "points": len(samples),
    }


def build_reading_from_cycle(cycle: CycleResult) -> BreathReading:
    """Turn a raw analyzer cycle into the reading the attendance service
    stores and displays, including the cannabis conformity score."""
    alcohol_value = _finite(cycle.alcohol.integral_mvs)
    cannabis_value = _finite(cycle.cannabis.integral_mvs)
    areas = area_ratio(cycle.cannabis.samples, BREATH_CANNABIS_THRESHOLD_MV)
    return BreathReading(
        alcohol_ppb=max(0.0, alcohol_value),
        cannabis_ppb=max(0.0, cannabis_value),
        alcohol_clear=alcohol_value <= BREATH_ALCOHOL_THRESHOLD_PPB,
        cannabis_clear=cannabis_value <= BREATH_CANNABIS_THRESHOLD_PPB,
        raw_sensor_value=_finite(cycle.cannabis.peak),
        cannabis_ratio=float(areas["ratio"]),
        cannabis_upper=float(areas["upper"]),
        cannabis_lower=float(areas["lower"]),
        cannabis_threshold_mv=float(areas["threshold"]),
        cannabis_points=int(areas["points"]),
        alcohol_baseline=round(cycle.alcohol.baseline / 1000.0, 3),   # nA -> uA
        alcohol_peak=round(cycle.alcohol.peak / 1000.0, 3),
        cannabis_baseline=round(cycle.cannabis.baseline * PID_MV_PER_LSB, 3),
        cannabis_peak=round(cycle.cannabis.peak * PID_MV_PER_LSB, 3),
        baseline_stable=bool(cycle.alcohol.stable and cycle.cannabis.stable),
    )


def build_breath_reading(
    alcohol_ppb: float,
    cannabis_ppb: float,
    raw_sensor_value: Optional[float] = None,
) -> BreathReading:
    """Minimal reading builder (no exhale trace, so no conformity score)."""
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


_pump_shutdown_hooks_installed = False
_live_analyzers: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _shutdown_live_analyzers() -> None:
    for analyzer in list(_live_analyzers):
        try:
            analyzer.shutdown()
        except Exception:
            pass


def _register_pump_shutdown(analyzer: Any) -> None:
    """Guarantee the pump is driven low when the process goes away.

    A released GPIO reverts to an undriven input, so without this a
    stop/restart or Ctrl-C mid-scan leaves the pump powered with nothing in
    control of it. Signal handlers are chained so uvicorn still gets its own
    graceful shutdown.
    """
    global _pump_shutdown_hooks_installed

    _live_analyzers.add(analyzer)
    if _pump_shutdown_hooks_installed:
        return
    _pump_shutdown_hooks_installed = True

    atexit.register(_shutdown_live_analyzers)

    # signal.signal only works on the main thread; a worker-thread import must
    # not crash, it simply relies on atexit and the FastAPI shutdown hook.
    if threading.current_thread() is not threading.main_thread():
        return

    def _install(signal_number: int) -> None:
        try:
            previous = signal.getsignal(signal_number)
        except (ValueError, OSError):
            return

        def _handler(received_number, frame):
            _shutdown_live_analyzers()
            if callable(previous) and previous not in (signal.SIG_IGN, signal.SIG_DFL):
                previous(received_number, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(received_number, signal.SIG_DFL)
                os.kill(os.getpid(), received_number)

        try:
            signal.signal(signal_number, _handler)
        except (ValueError, OSError):
            pass

    for signal_number in (signal.SIGTERM, signal.SIGINT):
        _install(signal_number)


class BreathAnalyzer:
    name = "base"
    startup_warnings: tuple[str, ...] = ()
    state = "ready"   # ready | stabilizing | measuring | finishing | error
    stream_ok = True  # False while the doorbell/frame stream is dead
    sample_seconds = BREATH_SAMPLE_SECONDS   # the blow window only

    @property
    def blow_delay_seconds(self) -> float:
        """Seconds of purge + baseline that run BEFORE the subject blows."""
        return max(0.0, BREATH_PURGE_SECONDS) + max(0.0, BREATH_BASELINE_SECONDS)

    @property
    def cycle_seconds(self) -> float:
        """Total wall time of one measurement cycle (purge + baseline + blow).
        Callers must wait this long for a reading, not just the blow window."""
        return self.blow_delay_seconds + max(1.0, float(self.sample_seconds))

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        raise NotImplementedError

    def read(self, worker_id: int, camera_id: str) -> BreathReading:
        """Attendance-facing entry point: run one full cycle for this blow."""
        del worker_id, camera_id
        cycle = self.run_cycle(self.sample_seconds)
        return build_reading_from_cycle(cycle)

    def stabilize(self) -> None:
        """App-start priming; no-op for the mock."""

    def shutdown(self) -> None:
        """Release hardware; no-op when there is none."""


def _mock_bell(measure_ms: float, baseline: float, peak_delta: float,
               step_ms: int = 100) -> tuple[tuple[int, float, float, float], ...]:
    """A positive bell-shaped exhale trace for development machines, shaped
    like the real PID response (rise, peak mid-blow, decay)."""
    centre = measure_ms * 0.45
    width = max(1.0, measure_ms * 0.22)
    samples = []
    for t_ms in range(0, int(measure_ms) + 1, step_ms):
        bell = math.exp(-((t_ms - centre) ** 2) / (2 * width * width))
        delta = peak_delta * bell + random.uniform(-0.4, 0.4)
        samples.append((t_ms, round(baseline + delta, 1), round(delta, 2),
                        sample_mv(SRC_AD7798, delta)))
    return tuple(samples)


class MockBreathAnalyzer(BreathAnalyzer):
    name = "mock"

    def __init__(self, startup_warnings: tuple[str, ...] = ()) -> None:
        self.startup_warnings = startup_warnings
        self.sample_seconds = BREATH_SAMPLE_SECONDS
        self._lock = threading.Lock()

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        with self._lock:
            self.state = "measuring"
            try:
                phases = (
                    ("purge", BREATH_PURGE_SECONDS),
                    ("baseline", BREATH_BASELINE_SECONDS),
                    ("measure", max(1.0, measure_seconds)),
                )
                for phase, total in phases:
                    started = time.monotonic()
                    while True:
                        elapsed = time.monotonic() - started
                        if progress:
                            progress(phase, min(elapsed, total), total)
                        if elapsed >= total:
                            break
                        time.sleep(0.15)

                alcohol_mvs = random.uniform(BREATH_MOCK_ALCOHOL_MIN, BREATH_MOCK_ALCOHOL_MAX)
                cannabis_mvs = random.uniform(BREATH_MOCK_CANNABIS_MIN, BREATH_MOCK_CANNABIS_MAX)
                measure_ms = max(1.0, measure_seconds) * 1000.0
                peak_ms = int((BREATH_PURGE_SECONDS + BREATH_BASELINE_SECONDS
                               + measure_seconds * random.uniform(0.3, 0.7)) * 1000)
                cannabis_baseline = round(random.uniform(400, 800), 1)
                cannabis_peak = round(cannabis_mvs * 40, 1)
                return CycleResult(
                    alcohol=ChannelResult(
                        baseline=round(random.uniform(300, 1500), 1),        # nA
                        peak=round(alcohol_mvs * 250, 1),                    # nA
                        peak_t_ms=peak_ms,
                        integral_mvs=round(alcohol_mvs, 3),
                    ),
                    cannabis=ChannelResult(
                        baseline=cannabis_baseline,                          # codes
                        peak=cannabis_peak,                                  # codes
                        peak_t_ms=peak_ms,
                        integral_mvs=round(cannabis_mvs, 3),
                        samples=_mock_bell(measure_ms, cannabis_baseline, cannabis_peak),
                    ),
                )
            finally:
                self.state = "ready"


class SpiBreathAnalyzer(BreathAnalyzer):
    name = "spi"

    def __init__(self, periphery_module: Optional[Any] = None) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        self._lock = threading.Lock()
        self.sample_seconds = BREATH_SAMPLE_SECONDS
        self.startup_warnings = (
            "Readings are uncalibrated integrals (mV*s) — set limits after calibration.",
        )
        self.last_stabilize: dict[str, Any] = {}
        self.stabilize_started_at: Optional[float] = None

        # Request BRD_ON atomically high, open the bus, then perform one
        # deliberate reset so every backend start begins from a known STM32
        # state. The stabilize pass below rebuilds its zero/baseline state.
        self.trigger = self.periphery.GPIO(BREATH_GPIO_CHIP, BREATH_BOARD_ENABLE_GPIO, "high")
        self.ready = self.periphery.GPIO(BREATH_GPIO_CHIP, BREATH_READY_GPIO, "in", edge="falling")
        # Request the pump as an output ALREADY DRIVEN LOW in one atomic step.
        # A plain "out" request leaves the initial level to the driver and this
        # line comes up HIGH, which starts the pump the instant the app opens
        # it. Opening the SPI bus before the pump is known-off would stretch
        # that window by however long the bus takes to come up, so the bus is
        # opened only afterwards.
        self.pump = self.periphery.GPIO(BREATH_GPIO_CHIP, BREATH_PUMP_GPIO, "low")
        self.pump.write(False)
        self.spi = self.periphery.SPI(BREATH_SPI_DEVICE, BREATH_SPI_MODE, BREATH_SPI_SPEED_HZ)
        # Register before the first board access: if _reset_board() raises,
        # resolve_breath_analyzer() falls back to the mock and this instance is
        # discarded, so without this the lines would be left open with BRD_ON
        # still high and nothing able to drive the pump low.
        _register_pump_shutdown(self)
        self._reset_board()
        self._last_frame_at = time.monotonic()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    # ---- frame layer -----------------------------------------------------

    def _reset_board(self) -> None:
        """Reset the STM32/AFE and wait briefly for its boot sequence."""
        self.pump.write(False)
        self.trigger.write(False)
        time.sleep(BREATH_BOARD_RESET_SECONDS)
        self.trigger.write(True)
        time.sleep(BREATH_BOARD_BOOT_SECONDS)

    def _exchange(self, cmd: int) -> tuple[Optional[list[tuple[int, int, int]]], Optional[str]]:
        tx = [cmd] + [0x00] * (FRAME_MAX - 1)
        rx = bytes(self.spi.transfer(tx))   # ONE full-duplex 246-byte transfer
        if rx[0] != 0xAA or rx[1] != 0x55:
            return None, f"bad header {rx[0]:02X} {rx[1]:02X}"
        if crc16_ccitt(rx[:-2]) != (rx[-2] << 8) | rx[-1]:
            return None, "bad CRC"
        records = []
        for i in range(min(rx[2], MAX_RECORDS)):
            # SensorRecord: uint32 tick(ms), uint16 src, 2 pad, int32 val (LE)
            records.append(struct.unpack_from("<IH2xi", rx, 4 + i * RECORD_SIZE))
        return records, None

    def _wait_frame(self, cmd: int = CMD_NONE,
                    timeout: Optional[float] = None) -> tuple[Optional[list[tuple[int, int, int]]], bool]:
        """Block for the next doorbell, exchange one frame carrying `cmd`.
        Returns (records, delivered). On a corrupt frame the STM32 may or may
        not have latched the command — treated as NOT delivered (commands are
        idempotent, so re-sending is safe)."""
        if timeout is None:
            timeout = BREATH_DOORBELL_TIMEOUT_SECONDS
        if self.ready.read():   # idle high: wait for a falling edge
            # Drop stale queued edges first — answering a doorbell whose
            # 100 ms window has passed exchanges garbage with the STM32.
            while self.ready.poll(0):
                self.ready.read_event()
            if self.ready.read():
                if not self.ready.poll(timeout):
                    return None, False
                self.ready.read_event()
        records, error = self._exchange(cmd)
        if error is None:
            self._last_frame_at = time.monotonic()
            self.stream_ok = True
        # Let the STM32 deassert. PID lamp startup keeps the STM32 busy well
        # past a second, so a slow deassert after a VALID frame must not fail
        # the exchange — the command was already latched. The bound exists
        # only so a wedged board cannot hang the thread forever.
        deadline = time.monotonic() + BREATH_DOORBELL_TIMEOUT_SECONDS
        while not self.ready.read():
            if time.monotonic() >= deadline:
                if error is None:
                    logger.warning("doorbell still asserted %.1fs after a valid frame",
                                   BREATH_DOORBELL_TIMEOUT_SECONDS)
                    break
                return None, False
            time.sleep(0.001)
        if error:
            return None, False
        return records, True

    def _send_commands(self, commands: list[int], max_tries: int = 15,
                       timeout: Optional[float] = None) -> bool:
        """Deliver each command on its own frame, retrying on frame errors.
        Each command gets its own retry budget: a slow PID start must not use
        all the attempts before the alcohol AFE start command is sent."""
        for command in commands:
            for _attempt in range(max_tries):
                _records, delivered = self._wait_frame(command, timeout=timeout)
                if delivered:
                    break
            else:
                logger.warning("command 0x%02X not delivered after %d frames",
                               command, max_tries)
                return False
        return True

    # ---- idle keepalive ---------------------------------------------------

    def _keepalive_loop(self) -> None:
        """Answer doorbells between cycles.

        The protocol obliges the host to answer every doorbell within 100 ms.
        stabilize()/run_cycle() honour that while they hold the lock; this
        thread covers the idle gaps so the frame stream never desyncs, and
        revives the board on its own if the stream goes quiet."""
        while True:
            time.sleep(0.02)
            if not self._lock.acquire(timeout=1.0):
                continue   # a cycle owns the bus and is servicing doorbells
            try:
                if self.state != "ready":
                    continue
                self._wait_frame(timeout=0.25)
                if time.monotonic() - self._last_frame_at > BREATH_STREAM_DEAD_SECONDS:
                    self._recover_stream()
            except Exception:
                pass   # never let the keepalive die; next pass retries
            finally:
                self._lock.release()

    def _recover_stream(self) -> None:
        """Reset a silent board and restart AFE sampling in the background,
        so the next scan starts on a live doorbell instead of failing."""
        logger.warning("frame stream dead for %.0fs — resetting sensor board",
                       BREATH_STREAM_DEAD_SECONDS)
        self._reset_board()
        self.stream_ok = self._send_commands(
            [CMD_PID_SHUTDOWN, CMD_AFE_STARTUP], max_tries=8, timeout=0.5)
        logger.warning("sensor board recovery %s",
                       "succeeded" if self.stream_ok else "FAILED")
        self._last_frame_at = time.monotonic()

    # ---- stabilize (app-start priming) -----------------------------------

    def stabilize(self) -> None:
        with self._lock:
            self.state = "stabilizing"
            self.stabilize_started_at = time.time()
            samples: list[tuple[int, int]] = []   # (stm32_ms, nA)
            settled = False
            recovered = False
            error: Optional[str] = None
            started = self.stabilize_started_at
            try:
                self.pump.write(False)   # priming happens in still air
                startup_commands = [CMD_PID_SHUTDOWN, CMD_AFE_STARTUP]
                if not self._send_commands(startup_commands, max_tries=3):
                    recovered = True
                    self._reset_board()
                    if not self._send_commands(startup_commands):
                        raise RuntimeError("sensor board produced no frames after hardware reset")
                last_eval = 0.0
                ok_streak = 0
                while time.time() - started < BREATH_STABILIZE_MAX_S:
                    records, _ = self._wait_frame()
                    if records is None:
                        continue
                    for tick, source, value in records:
                        if source == SRC_AD5941:
                            samples.append((tick, value))
                    if not samples or time.time() - last_eval < 1.0:
                        continue
                    last_eval = time.time()
                    t_now = samples[-1][0]
                    window = [s for s in samples if t_now - s[0] <= BREATH_SETTLE_WINDOW_MS]
                    if len(window) < 8 or (t_now - window[0][0]) < BREATH_SETTLE_WINDOW_MS * 0.8:
                        continue
                    mid = (t_now + window[0][0]) / 2.0
                    first = [v for t, v in window if t <= mid]
                    second = [v for t, v in window if t > mid]
                    if not first or not second:
                        continue
                    slope = ((sum(second) / len(second)) - (sum(first) / len(first))) \
                        / (BREATH_SETTLE_WINDOW_MS / 2000.0)   # nA/s
                    if abs(slope) <= BREATH_SETTLE_SLOPE_NA_S:
                        ok_streak += 1
                        if ok_streak >= 2:
                            settled = True
                            break
                    else:
                        ok_streak = 0
            except Exception as exc:
                error = str(exc)
            finally:
                # Keep AFE sampling alive between tests. The deployed board
                # does not emit the documented SYS keepalive while both
                # channels are off; shutting AFE down here leaves no doorbell
                # frame on which the next START command can be delivered.
                try:
                    self.pump.write(False)
                except Exception:
                    pass
                self.last_stabilize = {
                    "settled": settled,
                    "final_ua": round(samples[-1][1] / 1000.0, 3) if samples else None,
                    "elapsed_s": round(time.time() - started, 1),
                    "hardware_reset": recovered,
                }
                if error:
                    self.last_stabilize["error"] = error
                self.stabilize_started_at = None
                self.state = "ready"

    # ---- measurement cycle ------------------------------------------------

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        with self._lock:
            self.state = "measuring"
            purge_ms = BREATH_PURGE_SECONDS * 1000.0
            baseline_ms = BREATH_BASELINE_SECONDS * 1000.0
            measure_ms = max(1.0, measure_seconds) * 1000.0
            total_ms = purge_ms + baseline_ms + measure_ms

            stats = {s: {"base": [], "baseline": None, "integ": 0.0,
                         "peak": 0.0, "peak_t": 0, "prev": None, "stable": True,
                         "samples": []}
                     for s in (SRC_AD7798, SRC_AD5941)}
            t0: Optional[int] = None   # STM32 tick of first AD5941 sample
            try:
                if progress:
                    progress("starting", 0.0, 0.0)
                self.pump.write(True)   # active high
                startup_commands = [CMD_PID_STARTUP, CMD_AFE_STARTUP]
                if not self._send_commands(startup_commands, max_tries=3):
                    # Recover a board whose frame stream stopped unexpectedly.
                    logger.warning("scan startup commands undelivered — hardware reset mid-scan")
                    if progress:
                        progress("recovering", 0.0, 0.0)
                    self._reset_board()
                    self.pump.write(True)
                    if not self._send_commands(startup_commands):
                        raise RuntimeError("sensor board produced no frames after hardware reset")

                # Command delivery can legitimately take a few seconds while
                # the board wakes. Do not charge that time to the measurement
                # deadline or the first scan can fail after partially starting
                # the sensor, only for an immediate retry to work.
                wall_deadline = time.monotonic() + total_ms / 1000.0 + 30.0
                if progress:
                    progress("purge", 0.0, purge_ms / 1000.0)

                while True:
                    if time.monotonic() > wall_deadline:
                        raise RuntimeError("sensor board stopped responding mid-cycle")
                    records, _ = self._wait_frame()
                    if records is None:
                        continue
                    for tick, source, value in records:
                        if t0 is None and source == SRC_AD5941:
                            t0 = tick   # anchor on first alcohol sample
                        if source not in stats or t0 is None or tick < t0:
                            continue
                        dt = tick - t0
                        if dt >= total_ms:   # cycle complete
                            return self._build_result(stats)
                        channel = stats[source]
                        if dt < purge_ms:
                            phase, elapsed, total = "purge", dt / 1000.0, purge_ms / 1000.0
                        elif dt < purge_ms + baseline_ms:
                            phase = "baseline"
                            elapsed, total = (dt - purge_ms) / 1000.0, baseline_ms / 1000.0
                            channel["base"].append(value)
                        else:
                            phase = "measure"
                            elapsed, total = (dt - purge_ms - baseline_ms) / 1000.0, measure_ms / 1000.0
                            if channel["baseline"] is None and channel["base"]:
                                channel["baseline"] = sum(channel["base"]) / float(len(channel["base"]))
                                spread = max(channel["base"]) - min(channel["base"])
                                channel["stable"] = spread <= BASELINE_SPREAD_WARN[source]
                            if channel["baseline"] is not None:
                                delta = value - channel["baseline"]
                                if channel["prev"] is not None:
                                    prev_t, prev_d = channel["prev"]
                                    channel["integ"] += (delta + prev_d) / 2.0 * (tick - prev_t)
                                if delta > channel["peak"]:
                                    channel["peak"], channel["peak_t"] = delta, dt
                                channel["prev"] = (tick, delta)
                                # Exhale trace, timed from the start of the blow.
                                channel["samples"].append((
                                    int(dt - purge_ms - baseline_ms), float(value),
                                    float(delta), sample_mv(source, delta),
                                ))
                        if progress:
                            progress(phase, elapsed, total)
            finally:
                # The test itself is over; the PID-off handshake below can
                # take seconds (lamp shutdown makes the STM32 busy). Expose
                # "finishing" so scan screens don't read it as a live test —
                # a new scan started now simply queues behind this lock.
                self.state = "finishing"
                # Stop the pump FIRST. It is a direct GPIO write that needs no
                # board communication, whereas _send_commands retries up to 15
                # frames and each frame can burn ~5s waiting for a doorbell
                # plus ~5s on the deassert bound — against a slow or wedged
                # board that left the pump running for minutes after the blow.
                try:
                    self.pump.write(False)
                except Exception:
                    pass
                # Shut down the PID lamp, but deliberately leave AFE sampling
                # on so its doorbell frames can carry the next PID START.
                try:
                    self._send_commands([CMD_PID_SHUTDOWN])
                except Exception:
                    pass
                self.state = "ready"

    def shutdown(self) -> None:
        """Drive the pump and board-enable low, then release the lines.

        Without this, stopping the service (or Ctrl-C) mid-scan just lets the
        kernel release the GPIO: it reverts to an undriven input and the pump
        keeps running with no software in control.
        """
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        for line in ("pump", "trigger"):
            gpio = getattr(self, line, None)
            if gpio is None:
                continue
            try:
                gpio.write(False)
            except Exception:
                pass
        for resource in ("pump", "trigger", "ready", "spi"):
            handle = getattr(self, resource, None)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass

    def _build_result(self, stats: dict) -> CycleResult:
        def channel(source: int) -> ChannelResult:
            data = stats[source]
            baseline = data["baseline"] if data["baseline"] is not None else 0.0
            return ChannelResult(
                baseline=_finite(round(baseline, 1)),
                peak=_finite(round(data["peak"], 1)),
                peak_t_ms=int(data["peak_t"]),
                integral_mvs=_finite(round(_mvs(source, data["integ"]), 3)),
                stable=bool(data["stable"]),
                samples=tuple(data["samples"]),
            )
        return CycleResult(alcohol=channel(SRC_AD5941), cannabis=channel(SRC_AD7798))


# Backwards-compatible alias: the previous implementation exposed this name.
LiveSpiBreathAnalyzer = SpiBreathAnalyzer


class PumpGuard:
    """Holds the breath pump line LOW whenever the sensor driver is not.

    Without this the pump is only ever controlled in `spi` mode. In `mock`
    mode — which is the default, and what `app.py demo`/`kiosk` run — no GPIO
    is opened at all, so a pump left running by a previous process, another
    service, or the board's power-on default keeps running and nothing in this
    app can stop it.

    The line is claimed as an output already driven low and HELD for the life
    of the process: closing it would release the line back to an undriven
    input, where it can float high again.
    """

    def __init__(self, periphery_module: Optional[Any] = None) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        self.pump = self.periphery.GPIO(BREATH_GPIO_CHIP, BREATH_PUMP_GPIO, "low")
        self.pump.write(False)
        self._shut_down = False

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        try:
            self.pump.write(False)
        except Exception:
            pass
        try:
            self.pump.close()
        except Exception:
            pass


def _guard_pump_when_sensor_is_idle() -> tuple[str, ...]:
    """Force the pump off in non-SPI modes. Returns any startup warnings."""
    if os.getenv("ATTENDANCE_BREATH_PUMP_GUARD", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ()
    try:
        guard = PumpGuard()
    except Exception as exc:
        # No periphery, no such GPIO chip, or the line is held by another
        # process. Nothing to guard on this machine — say so rather than
        # pretending the pump is under control.
        return (f"Breath pump line could not be forced off ({exc}).",)

    _register_pump_shutdown(guard)
    logger.info("breath pump line %s held low (sensor idle)", BREATH_PUMP_GPIO)
    return ()


def resolve_breath_analyzer() -> BreathAnalyzer:
    if BREATH_ANALYZER_MODE in {"spi", "live", "hardware"}:
        try:
            return SpiBreathAnalyzer()
        except Exception as exc:
            return MockBreathAnalyzer(
                startup_warnings=(
                    f"SPI breath board unavailable ({exc}). Using mock readings.",
                ) + _guard_pump_when_sensor_is_idle()
            )
    if BREATH_ANALYZER_MODE == "mock":
        return MockBreathAnalyzer(startup_warnings=_guard_pump_when_sensor_is_idle())
    return MockBreathAnalyzer(
        startup_warnings=(
            f"Unsupported breath analyzer mode '{BREATH_ANALYZER_MODE}'. Falling back to mock readings.",
        ) + _guard_pump_when_sensor_is_idle()
    )
