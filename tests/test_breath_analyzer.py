from __future__ import annotations

import unittest

from src.breath_analyzer import LiveSpiBreathAnalyzer, LiveSpiSettings


class _FakeGPIO:
    instances: list["_FakeGPIO"] = []

    def __init__(self, pin: int, direction: str) -> None:
        self.pin = pin
        self.direction = direction
        self.writes: list[bool] = []
        self.closed = False
        type(self).instances.append(self)

    def write(self, value: bool) -> None:
        self.writes.append(bool(value))

    def close(self) -> None:
        self.closed = True


class _FakeSPI:
    instances: list["_FakeSPI"] = []

    def __init__(self, device: str, mode: int, speed_hz: int) -> None:
        self.device = device
        self.mode = mode
        self.speed_hz = speed_hz
        self.commands: list[int] = []
        self.closed = False
        type(self).instances.append(self)

    def transfer(self, tx: list[int]) -> list[int]:
        command = int(tx[0])
        self.commands.append(command)
        if command == 0x30:
            return [command, 0x01, 0x90, 0x00]
        return [command, 0x00, 0x00, 0x00]

    def close(self) -> None:
        self.closed = True


class _FakePeriphery:
    GPIO = _FakeGPIO
    SPI = _FakeSPI


class BreathAnalyzerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeGPIO.instances.clear()
        _FakeSPI.instances.clear()

    def test_live_spi_reader_converts_adc_signal_to_cannabis_reading_and_shuts_down_board(self) -> None:
        analyzer = LiveSpiBreathAnalyzer(
            settings=LiveSpiSettings(
                sample_seconds=0.0,
                sample_interval_seconds=0.0,
                alcohol_source="mock",
                placeholder_alcohol_min_ppb=4.0,
                placeholder_alcohol_max_ppb=4.0,
                adc_bits=16,
                adc_vref=2.5,
                adc_gain=2.0,
                cannabis_scale=1.0,
                cannabis_offset=0.0,
            ),
            periphery_module=_FakePeriphery,
        )

        reading = analyzer.read(worker_id=1, camera_id="gate-1")

        self.assertIsNone(reading.raw_sensor_value)
        self.assertAlmostEqual(reading.alcohol_ppb, 4.0)
        self.assertAlmostEqual(reading.cannabis_ppb, 400.0 * (2.5 / (65536.0 * 2.0)))
        self.assertTrue(reading.alcohol_clear)
        self.assertTrue(reading.cannabis_clear)
        self.assertEqual(_FakeGPIO.instances[0].writes, [True, False])
        self.assertTrue(_FakeGPIO.instances[0].closed)
        self.assertIn(0x10, _FakeSPI.instances[0].commands)
        self.assertIn(0x20, _FakeSPI.instances[0].commands)
        self.assertIn(0x30, _FakeSPI.instances[0].commands)
        self.assertIn(0x21, _FakeSPI.instances[0].commands)
        self.assertIn(0x11, _FakeSPI.instances[0].commands)
        self.assertTrue(_FakeSPI.instances[0].closed)

    def test_live_spi_reader_keeps_aggregated_adc_only_for_legacy_alcohol_conversion(self) -> None:
        analyzer = LiveSpiBreathAnalyzer(
            settings=LiveSpiSettings(
                sample_seconds=0.0,
                sample_interval_seconds=0.0,
                adc_baseline=100.0,
                alcohol_source="adc",
                alcohol_scale=0.1,
                alcohol_offset=0.0,
                adc_bits=16,
                adc_vref=2.5,
                adc_gain=2.0,
                cannabis_scale=1.0,
                cannabis_offset=0.0,
            ),
            periphery_module=_FakePeriphery,
        )

        reading = analyzer.read(worker_id=1, camera_id="gate-1")

        self.assertEqual(reading.raw_sensor_value, 400.0)
        self.assertAlmostEqual(reading.alcohol_ppb, 30.0)
        self.assertAlmostEqual(reading.cannabis_ppb, 400.0 * (2.5 / (65536.0 * 2.0)))


if __name__ == "__main__":
    unittest.main()
