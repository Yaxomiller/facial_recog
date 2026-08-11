#!/usr/bin/env python3
"""INA745B power monitor - continuous power-draw logging over I2C.

The INA745B is an I2C part (header pins 3/5 = SDA/SCL). It has no SPI
interface.

Purpose: capture real current/power draw so a buck converter can be sized
against measured peak and average load instead of a guessed margin. The
summary line reports running average, peak and the peak/average ratio, which
is the number that decides the headroom.

Usage
-----
  # find the device first
  ./scripts/power_monitor.py --scan

  # live table, 5 samples/second, board shunt value must be given
  ./scripts/power_monitor.py --shunt-ohms 0.010 --max-current 5

  # log to CSV as well
  ./scripts/power_monitor.py --shunt-ohms 0.010 --max-current 5 --csv power.csv

VERIFY BEFORE TRUSTING THE SCALED NUMBERS
-----------------------------------------
The register map and LSB constants below follow TI's 16-bit INA74x/INA23x
family. Confirm them against the INA745B datasheet for your silicon revision,
and confirm --shunt-ohms against the actual resistor fitted on the board: an
incorrect shunt value scales current and power linearly and silently.

Raw register values are printed alongside the scaled ones (--raw) precisely so
the scaling can be checked against a bench meter.
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
    INA745B,
    POWER_LSB_FACTOR,
    SHUNT_CAL_FACTOR,
    TI_MANUFACTURER_ID,
    VBUS_LSB_V,
    to_signed as _to_signed,
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
    parser = argparse.ArgumentParser(
        description="Log INA745B power draw over I2C.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bus", default="/dev/i2c-1",
                        help="I2C bus device (default: /dev/i2c-1)")
    parser.add_argument("--address", default="0x40",
                        help="I2C address, 0x40-0x4F (default: 0x40)")
    parser.add_argument("--shunt-ohms", type=float,
                        help="Shunt resistor fitted on the board, in ohms (e.g. 0.010)")
    parser.add_argument("--max-current", type=float, default=5.0,
                        help="Max expected current in amps; sets resolution (default: 5)")
    parser.add_argument("--adc-range", type=int, choices=(0, 1), default=0,
                        help="0 = +/-163.84mV shunt range (default), 1 = +/-40.96mV")
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

    if args.shunt_ohms is None:
        parser.error("--shunt-ohms is required: current and power scale directly "
                     "with it, so a guessed value gives silently wrong readings.")

    address = int(args.address, 0)
    monitor = INA745B(
        bus=args.bus,
        address=address,
        shunt_ohms=args.shunt_ohms,
        max_current_a=args.max_current,
        adc_range=args.adc_range,
    )

    try:
        manufacturer, device = monitor.identify()
    except Exception as exc:
        print(f"Could not reach a device at 0x{address:02X} on {args.bus}: {exc}")
        print("Run with --scan to see which addresses respond.")
        monitor.close()
        return 1

    print(f"Bus {args.bus}   address 0x{address:02X}")
    print(f"Manufacturer 0x{manufacturer:04X}"
          f"{'  (TI, as expected)' if manufacturer == TI_MANUFACTURER_ID else '  (UNEXPECTED - not a TI part?)'}"
          f"   device 0x{device:04X}")
    print(f"Shunt {args.shunt_ohms} ohm   max current {args.max_current} A   "
          f"resolution {monitor.current_lsb * 1e6:.1f} uA/LSB")
    print()

    monitor.configure()

    csv_writer = None
    csv_handle = None
    if args.csv:
        csv_handle = open(args.csv, "a", newline="")
        csv_writer = csv.writer(csv_handle)
        if csv_handle.tell() == 0:
            csv_writer.writerow(
                ["timestamp", "bus_v", "shunt_mv", "current_ma", "power_mw", "temp_c"]
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
                    f"{sample.shunt_v * 1000:.4f}", f"{sample.current_a * 1000:.3f}",
                    f"{sample.power_w * 1000:.3f}", f"{sample.temp_c:.2f}",
                ])
                csv_handle.flush()

            if args.duration and elapsed >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    finally:
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
        if args.csv:
            print(f"  csv              {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
