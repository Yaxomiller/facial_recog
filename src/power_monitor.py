"""INA745B power monitor - shared driver plus an in-app background sampler.

TEMPORARY TESTING FEATURE. Samples board power draw while the app runs so the
buck converter can be sized against measured load. Everything degrades to
"disabled, here is why" when the chip or the I2C bus is absent, so a board
without the monitor is unaffected.

The INA745B is an I2C part (header pins 3/5 = SDA/SCL); it has no SPI
interface.

Register map, LSB sizes and bit fields below are taken from the INA745A/B
datasheet (SBOSAC3B, revised August 2025), Tables 7-1 and 8-1.

Key property of this part: the shunt is INTEGRATED (800 uOhm kelvin) and the
device reports current and power already calculated, with FIXED LSB sizes.
There is no SHUNT_CAL register, no VSHUNT register and no ADCRANGE bit - those
belong to the external-shunt parts (INA228/237/238). Nothing here needs a
shunt value or a calibration step.

Grade B (the "B" in INA745B) is the higher-accuracy bin: +/-0.9% current and
+/-1.6% power at full scale, versus +/-1.4% / +/-2.1% for grade A. It changes
the accuracy specification only - the register map and scaling are identical.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
import threading
import time
from typing import Any, Optional

# --- INA745x register map (datasheet Table 7-1) ------------------------------
REG_CONFIG = 0x00           # 16-bit
REG_ADC_CONFIG = 0x01       # 16-bit
REG_VBUS = 0x05             # 16-bit signed, always positive
REG_DIETEMP = 0x06          # 12-bit signed in bits 15-4
REG_CURRENT = 0x07          # 16-bit signed
REG_POWER = 0x08            # 24-bit unsigned
REG_ENERGY = 0x09           # 40-bit unsigned accumulator
REG_CHARGE = 0x0A           # 40-bit unsigned accumulator
REG_DIAG_ALRT = 0x0B        # 16-bit
REG_MANUFACTURER_ID = 0x3E  # 16-bit, reset 0x5449 = "TI"

TI_MANUFACTURER_ID = 0x5449

# CONFIG bits
CONFIG_RST = 0x8000         # self-clearing system reset
CONFIG_RSTACC = 0x4000      # clear ENERGY and CHARGE accumulators

# ADC_CONFIG reset value FB68h = MODE Fh (continuous temperature, current and
# bus voltage) with 1052us conversion times. That is exactly what continuous
# profiling wants, so it is written back explicitly after a reset.
ADC_CONFIG_CONTINUOUS = 0xFB68

# Fixed LSB sizes (datasheet Table 8-1). These are NOT derived from a shunt
# value or a calibration register on this part.
VBUS_LSB_V = 3.125e-3       # 3.125 mV/LSB,  full scale 0-40 V
CURRENT_LSB_A = 1.2e-3      # 1.2 mA/LSB,    full scale +/-39.32 A
POWER_LSB_W = 240e-6        # 240 uW/LSB,    full scale 4026.53 W
DIETEMP_LSB_C = 125e-3      # 125 m degC/LSB, 12-bit field
ENERGY_LSB_J = 3.840e-3     # 3.840 mJ/LSB
CHARGE_LSB_C = 75e-6        # 75 uC/LSB

INTERNAL_SHUNT_OHMS = 800e-6  # integrated kelvin resistance, for reference


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
POWER_SAMPLE_INTERVAL = max(0.05, _env_float("ATTENDANCE_POWER_INTERVAL", 0.5))


def to_signed(value: int, bits: int) -> int:
    """Interpret an unsigned register value as two's-complement."""
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def decode_dietemp(raw: int) -> float:
    """DIETEMP holds a 12-bit signed value in bits 15-4; bits 3-0 read 0."""
    return to_signed(raw >> 4, 12) * DIETEMP_LSB_C


@dataclass(frozen=True)
class PowerSample:
    timestamp: float
    bus_v: float
    current_a: float
    power_w: float
    temp_c: float
    raw: dict


class INA745B:
    def __init__(
        self,
        bus: str = POWER_I2C_BUS,
        address: int = POWER_I2C_ADDRESS,
        periphery_module: Optional[Any] = None,
    ) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        self.i2c = self.periphery.I2C(bus)
        self.address = address

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

    def manufacturer_id(self) -> int:
        return self._read(REG_MANUFACTURER_ID)

    def configure(self) -> None:
        """Reset, then select continuous temperature + current + bus voltage."""
        self._write(REG_CONFIG, CONFIG_RST)
        time.sleep(0.01)
        self._write(REG_ADC_CONFIG, ADC_CONFIG_CONTINUOUS)
        time.sleep(0.05)

    def reset_accumulators(self) -> None:
        """Clear the ENERGY and CHARGE accumulators without a full reset."""
        self._write(REG_CONFIG, CONFIG_RSTACC)

    def read_sample(self) -> PowerSample:
        raw_vbus = self._read(REG_VBUS)
        raw_current = self._read(REG_CURRENT)
        raw_power = self._read(REG_POWER, 3)
        raw_temp = self._read(REG_DIETEMP)
        return PowerSample(
            timestamp=time.time(),
            bus_v=to_signed(raw_vbus, 16) * VBUS_LSB_V,
            current_a=to_signed(raw_current, 16) * CURRENT_LSB_A,
            power_w=raw_power * POWER_LSB_W,
            temp_c=decode_dietemp(raw_temp),
            raw={
                "vbus": raw_vbus,
                "current": raw_current,
                "power": raw_power,
                "dietemp": raw_temp,
            },
        )

    def read_energy_joules(self) -> float:
        return self._read(REG_ENERGY, 5) * ENERGY_LSB_J

    def read_charge_coulombs(self) -> float:
        return self._read(REG_CHARGE, 5) * CHARGE_LSB_C

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
            manufacturer = device.manufacturer_id()
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
        device = self._device
        if device is not None:
            try:
                device.reset_accumulators()
            except Exception:
                pass

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
                "internal_shunt_ohms": INTERNAL_SHUNT_OHMS,
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
