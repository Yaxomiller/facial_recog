#!/usr/bin/env python3
"""INA745B power monitor - continuous power-draw logging over I2C.

The INA745B is an I2C part (header pins 3/5 = SDA/SCL). It has no SPI
interface.

Purpose: capture real current/power draw so a buck converter can be sized
against measured peak and average load instead of a guessed margin. The
summary reports running average, peak and the peak/average ratio, which is the
number that decides the headroom.

No shunt value is needed: the INA745B has an INTEGRATED 800 uOhm shunt and
reports current and power already calculated, with fixed LSB sizes
(1.2 mA/LSB and 240 uW/LSB per datasheet Table 8-1).

Usage
-----
  # find the device first
  ./scripts/power_monitor.py --scan

  # live table, 5 samples/second
  ./scripts/power_monitor.py

  # log to CSV as well, stop after 10 minutes
  ./scripts/power_monitor.py --csv power.csv --duration 600
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# Single source of truth for the register map, LSB constants and the driver:
# src/power_monitor.py, which the running app uses too. Keeping one copy means
# a datasheet correction cannot land in only one of them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.power_monitor import (  # noqa: E402
    CURRENT_LSB_A,
    INA745B,
    INTERNAL_SHUNT_OHMS,
    POWER_LSB_W,
    TI_MANUFACTURER_ID,
    VBUS_LSB_V,
)


def scan(bus: str) -> None:
    """List responding I2C addresses. The INA745B answers on 0x40-0x4F."""
    try:
        from periphery import I2C
    except ImportError:
        raise SystemExit("python-periphery is required:  pip install python-periphery")

    i2c = I2C(bus)
    found = []
    for address in range(0x03, 0x78):
        try:
            i2c.transfer(address, [I2C.Message([0x00], read=True)])
            found.append(address)
        except Exception:
            continue
    i2c.close()

    if not found:
        print(f"No I2C devices responded on {bus}.")
        print("Check wiring, and that the bus is enabled (pins 3/5 = SDA/SCL).")
        return
    print(f"Devices on {bus}:")
    for address in found:
        note = "  <-- INA745B address range" if 0x40 <= address <= 0x4F else ""
        print(f"  0x{address:02X}{note}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Log INA745B power draw over I2C.")
    parser.add_argument("--bus", default="/dev/i2c-1",
                        help="I2C bus device (default: /dev/i2c-1)")
    parser.add_argument("--address", default="0x40",
                        help="I2C address, 0x40-0x4F (default: 0x40)")
    parser.add_argument("--interval", type=float, default=0.2,
                        help="Seconds between samples (default: 0.2)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Stop after N seconds (default: run until Ctrl+C)")
    parser.add_argument("--csv", help="Also append samples to this CSV file")
    parser.add_argument("--raw", action="store_true",
                        help="Show raw register values next to the scaled ones")
    parser.add_argument("--scan", action="store_true",
                        help="List I2C devices on the bus and exit")
    args = parser.parse_args()

    if args.scan:
        scan(args.bus)
        return 0

    address = int(args.address, 0)
    try:
        monitor = INA745B(bus=args.bus, address=address)
        manufacturer = monitor.manufacturer_id()
    except Exception as exc:
        print(f"Could not reach a device at 0x{address:02X} on {args.bus}: {exc}")
        print("Run with --scan to see which addresses respond.")
        return 1

    print(f"Bus {args.bus}   address 0x{address:02X}")
    if manufacturer == TI_MANUFACTURER_ID:
        print(f"Manufacturer 0x{manufacturer:04X} (TI, as expected)")
    else:
        print(f"Manufacturer 0x{manufacturer:04X} - UNEXPECTED, not a TI part? "
              f"Expected 0x{TI_MANUFACTURER_ID:04X}")
    print(f"Integrated shunt {INTERNAL_SHUNT_OHMS * 1e6:.0f} uOhm   "
          f"current {CURRENT_LSB_A * 1000:.1f} mA/LSB   power {POWER_LSB_W * 1e6:.0f} uW/LSB")
    print()

    monitor.configure()

    csv_writer = None
    csv_handle = None
    if args.csv:
        csv_handle = open(args.csv, "a", newline="")
        csv_writer = csv.writer(csv_handle)
        if csv_handle.tell() == 0:
            csv_writer.writerow(
                ["timestamp", "bus_v", "current_ma", "power_mw", "temp_c"]
            )

    header = f"{'time':>8}  {'bus V':>8}  {'current mA':>11}  {'power mW':>10}  {'temp C':>7}"
    if args.raw:
        header += f"  {'raw_vbus':>9} {'raw_cur':>8} {'raw_pwr':>9}"
    print(header)
    print("-" * len(header))

    started = time.time()
    count = 0
    total_w = 0.0
    peak_w = 0.0
    peak_a = 0.0

    try:
        while True:
            sample = monitor.read_sample()
            elapsed = sample.timestamp - started

            count += 1
            total_w += sample.power_w
            peak_w = max(peak_w, sample.power_w)
            peak_a = max(peak_a, abs(sample.current_a))

            line = (f"{elapsed:8.1f}  {sample.bus_v:8.3f}  "
                    f"{sample.current_a * 1000:11.2f}  {sample.power_w * 1000:10.2f}  "
                    f"{sample.temp_c:7.1f}")
            if args.raw:
                line += (f"  {sample.raw['vbus']:9d} {sample.raw['current']:8d} "
                         f"{sample.raw['power']:9d}")
            print(line, flush=True)

            if csv_writer:
                csv_writer.writerow([
                    f"{sample.timestamp:.3f}", f"{sample.bus_v:.4f}",
                    f"{sample.current_a * 1000:.3f}", f"{sample.power_w * 1000:.3f}",
                    f"{sample.temp_c:.2f}",
                ])
                csv_handle.flush()

            if args.duration and elapsed >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    finally:
        try:
            energy_j = monitor.read_energy_joules()
        except Exception:
            energy_j = None
        monitor.close()
        if csv_handle:
            csv_handle.close()

    if count:
        average_w = total_w / count
        print()
        print("Summary - size the buck against these, not a guess")
        print(f"  samples          {count}")
        print(f"  average power    {average_w * 1000:.1f} mW")
        print(f"  peak power       {peak_w * 1000:.1f} mW")
        print(f"  peak current     {peak_a * 1000:.1f} mA")
        if average_w > 0:
            print(f"  peak / average   {peak_w / average_w:.2f}x")
        if energy_j is not None:
            print(f"  energy (device)  {energy_j:.3f} J")
        if args.csv:
            print(f"  csv              {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
