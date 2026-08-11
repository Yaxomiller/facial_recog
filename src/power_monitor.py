"""INA745B power monitor - shared driver plus an in-app background sampler.

TEMPORARY TESTING FEATURE. Samples board power draw while the app runs so the
buck converter can be sized against measured load. Everything degrades to
"disabled, here is why" when the chip or the I2C bus is absent, so a board
without the monitor is unaffected.

The INA745B is an I2C part (header pins 3/5 = SDA/SCL); it has no SPI
interface.

VERIFY BEFORE TRUSTING THE SCALED NUMBERS: the register map and LSB constants
follow TI's 16-bit INA74x/INA23x family and should be confirmed against the
INA745B datasheet. ATTENDANCE_POWER_SHUNT_OHMS must match the resistor
actually fitted -- current and power scale linearly with it.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
import threading
import time
from typing import Any, Optional

# --- INA745B register map (16-bit unless noted) ------------------------------
REG_CONFIG = 0x00
REG_ADC_CONFIG = 0x01
REG_SHUNT_CAL = 0x02
REG_VSHUNT = 0x04
REG_VBUS = 0x05
REG_DIETEMP = 0x06
REG_CURRENT = 0x07
REG_POWER = 0x08          # 24-bit, unsigned
REG_MANUFACTURER_ID = 0x3E   # expected 0x5449 = "TI"
REG_DEVICE_ID = 0x3F

TI_MANUFACTURER_ID = 0x5449

VBUS_LSB_V = 3.125e-3        # 3.125 mV
VSHUNT_LSB_V_RANGE0 = 5e-6   # 5 uV
VSHUNT_LSB_V_RANGE1 = 1.25e-6
DIETEMP_LSB_C = 125e-3       # 125 m degC
POWER_LSB_FACTOR = 3.2       # POWER_LSB = 3.2 * CURRENT_LSB
SHUNT_CAL_FACTOR = 819.2e6   # SHUNT_CAL = 819.2e6 * CURRENT_LSB * R_shunt


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


POWER_MONITOR_ENABLED = _env("ATTENDANCE_POWER_MONITOR", "1").lower() in {"1", "true", "yes", "on"}
POWER_I2C_BUS = _env("ATTENDANCE_POWER_I2C_BUS", "/dev/i2c-1")
POWER_I2C_ADDRESS = int(_env("ATTENDANCE_POWER_I2C_ADDRESS", "0x40"), 0)
POWER_SHUNT_OHMS = _env_float("ATTENDANCE_POWER_SHUNT_OHMS", 0.010)
POWER_MAX_CURRENT_A = _env_float("ATTENDANCE_POWER_MAX_CURRENT", 5.0)
POWER_ADC_RANGE = int(_env("ATTENDANCE_POWER_ADC_RANGE", "0"))
POWER_SAMPLE_INTERVAL = max(0.05, _env_float("ATTENDANCE_POWER_INTERVAL", 0.5))


def to_signed(value: int, bits: int) -> int:
    """Interpret an unsigned register value as two's-complement."""
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


@dataclass(frozen=True)
class PowerSample:
    timestamp: float
    bus_v: float
    shunt_v: float
    current_a: float
    power_w: float
    temp_c: float
    raw: dict


class INA745B:
    def __init__(
        self,
        bus: str = POWER_I2C_BUS,
        address: int = POWER_I2C_ADDRESS,
        shunt_ohms: float = POWER_SHUNT_OHMS,
        max_current_a: float = POWER_MAX_CURRENT_A,
        adc_range: int = POWER_ADC_RANGE,
        periphery_module: Optional[Any] = None,
    ) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        self.i2c = self.periphery.I2C(bus)
        self.address = address
        self.shunt_ohms = shunt_ohms
        self.adc_range = adc_range
        self.vshunt_lsb = VSHUNT_LSB_V_RANGE1 if adc_range else VSHUNT_LSB_V_RANGE0
        # CURRENT_LSB sets resolution: full scale is a signed 15-bit count.
        self.current_lsb = max_current_a / (2 ** 15)
        self.power_lsb = POWER_LSB_FACTOR * self.current_lsb

    def _read(self, register: int, length: int = 2) -> int:
        message = self.periphery.I2C.Message
        write = message([register])
        read = message([0] * length, read=True)
        self.i2c.transfer(self.address, [write, read])
        value = 0
        for byte in read.data:
            value = (value << 8) | byte
        return value

    def _write(self, register: int, value: int) -> None:
        message = self.periphery.I2C.Message
        payload = [register, (value >> 8) & 0xFF, value & 0xFF]
        self.i2c.transfer(self.address, [message(payload)])

    def identify(self) -> tuple[int, int]:
        return self._read(REG_MANUFACTURER_ID), self._read(REG_DEVICE_ID)

    def configure(self) -> None:
        self._write(REG_CONFIG, 0x8000)          # reset
        time.sleep(0.01)
        if self.adc_range:
            self._write(REG_CONFIG, 0x0010)      # ADCRANGE = 1

        shunt_cal = int(SHUNT_CAL_FACTOR * self.current_lsb * self.shunt_ohms)
        if self.adc_range:
            shunt_cal *= 4
        self._write(REG_SHUNT_CAL, max(0, min(0xFFFF, shunt_cal)))

        # continuous bus+shunt+temp, 1052us conversions, 16x averaging
        self._write(REG_ADC_CONFIG, 0xFB6A)
        time.sleep(0.05)

    def read_sample(self) -> PowerSample:
        raw_vbus = self._read(REG_VBUS)
        raw_vshunt = self._read(REG_VSHUNT)
        raw_current = self._read(REG_CURRENT)
        raw_power = self._read(REG_POWER, 3)
        raw_temp = self._read(REG_DIETEMP)
        return PowerSample(
            timestamp=time.time(),
            bus_v=to_signed(raw_vbus, 16) * VBUS_LSB_V,
            shunt_v=to_signed(raw_vshunt, 16) * self.vshunt_lsb,
            current_a=to_signed(raw_current, 16) * self.current_lsb,
            power_w=raw_power * self.power_lsb,
            temp_c=to_signed(raw_temp, 16) * DIETEMP_LSB_C,
            raw={
                "vbus": raw_vbus,
                "vshunt": raw_vshunt,
                "current": raw_current,
                "power": raw_power,
                "dietemp": raw_temp,
            },
        )

    def close(self) -> None:
        try:
            self.i2c.close()
        except Exception:
            pass


class PowerMonitor:
    """Background sampler. Never raises: if the hardware is missing it reports
    disabled with the reason, so the app is unaffected on boards without it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._device: Optional[INA745B] = None
        self._latest: Optional[PowerSample] = None
        self._samples = 0
        self._total_w = 0.0
        self._peak_w = 0.0
        self._peak_a = 0.0
        self._started_at = 0.0
        self.enabled = False
        self.reason = "not started"

    def start(self) -> None:
        if not POWER_MONITOR_ENABLED:
            self.reason = "disabled by ATTENDANCE_POWER_MONITOR"
            return
        if self._thread is not None and self._thread.is_alive():
            return

        try:
            device = INA745B()
            manufacturer, _device_id = device.identify()
            if manufacturer != TI_MANUFACTURER_ID:
                device.close()
                self.reason = (
                    f"device at {POWER_I2C_BUS} 0x{POWER_I2C_ADDRESS:02X} reported "
                    f"manufacturer 0x{manufacturer:04X}, expected 0x{TI_MANUFACTURER_ID:04X} (TI)"
                )
                return
            device.configure()
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"
            return

        self._device = device
        self._stop.clear()
        self._started_at = time.time()
        self.enabled = True
        self.reason = ""
        self._thread = threading.Thread(target=self._loop, daemon=True, name="power-monitor")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._device.read_sample()
            except Exception as exc:
                with self._lock:
                    self.enabled = False
                    self.reason = f"read failed: {exc}"
                return
            with self._lock:
                self._latest = sample
                self._samples += 1
                self._total_w += sample.power_w
                self._peak_w = max(self._peak_w, sample.power_w)
                self._peak_a = max(self._peak_a, abs(sample.current_a))
            self._stop.wait(POWER_SAMPLE_INTERVAL)

    def reset_statistics(self) -> None:
        with self._lock:
            self._samples = 0
            self._total_w = 0.0
            self._peak_w = 0.0
            self._peak_a = 0.0
            self._started_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            if not self.enabled or self._latest is None:
                return {
                    "enabled": False,
                    "reason": self.reason or "no samples yet",
                    "bus": POWER_I2C_BUS,
                    "address": f"0x{POWER_I2C_ADDRESS:02X}",
                }
            average_w = self._total_w / self._samples if self._samples else 0.0
            return {
                "enabled": True,
                "reason": "",
                "bus": POWER_I2C_BUS,
                "address": f"0x{POWER_I2C_ADDRESS:02X}",
                "shunt_ohms": POWER_SHUNT_OHMS,
                "bus_v": round(self._latest.bus_v, 4),
                "current_ma": round(self._latest.current_a * 1000, 2),
                "power_mw": round(self._latest.power_w * 1000, 2),
                "temp_c": round(self._latest.temp_c, 1),
                "average_mw": round(average_w * 1000, 2),
                "peak_mw": round(self._peak_w * 1000, 2),
                "peak_ma": round(self._peak_a * 1000, 2),
                # The number that decides buck headroom.
                "peak_ratio": round(self._peak_w / average_w, 2) if average_w > 0 else 0.0,
                "samples": self._samples,
                "elapsed_s": round(time.time() - self._started_at, 1),
            }

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if self._device is not None:
            self._device.close()
            self._device = None
        self.enabled = False
