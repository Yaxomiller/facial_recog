from __future__ import annotations

import os
from typing import Optional
import unittest
from unittest.mock import patch

from src.native_react_app import (
    _get_backend_startup_timeout_seconds,
    _get_native_backend_port,
    _native_app_url,
    _wait_for_backend_ready,
)


class _StubProcess:
    def __init__(self, polls: list[Optional[int]]) -> None:
        self._polls = list(polls)

    def poll(self) -> Optional[int]:
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]


class NativeReactAppTests(unittest.TestCase):
    def test_native_app_url_points_to_backend_root(self) -> None:
        self.assertEqual(_native_app_url(), "http://127.0.0.1:8000/")

    def test_get_native_backend_port_uses_env_override(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_NATIVE_BACKEND_PORT": "8123"}, clear=False):
            with patch("src.native_react_app._port_is_available", return_value=True):
                self.assertEqual(_get_native_backend_port(), 8123)

    def test_get_native_backend_port_uses_free_local_port_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATTENDANCE_NATIVE_BACKEND_PORT", None)
            with patch("src.native_react_app._find_free_local_port", return_value=8542):
                self.assertEqual(_get_native_backend_port(), 8542)

    def test_get_native_backend_port_rejects_busy_override(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_NATIVE_BACKEND_PORT": "8123"}, clear=False):
            with patch("src.native_react_app._port_is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    _get_native_backend_port()

    def test_backend_startup_timeout_uses_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS", None)
            self.assertEqual(_get_backend_startup_timeout_seconds(), 45.0)

    def test_backend_startup_timeout_uses_env_override(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS": "90"}, clear=False):
            self.assertEqual(_get_backend_startup_timeout_seconds(), 90.0)

    def test_backend_startup_timeout_rejects_invalid_value(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS": "fast"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS must be a number."):
                _get_backend_startup_timeout_seconds()

    def test_wait_for_backend_ready_stops_when_process_exits(self) -> None:
        process = _StubProcess([None, 1])
        readiness_checks = iter([False, False, False])

        ready = _wait_for_backend_ready(
            timeout_seconds=1.0,
            backend_process=process,
            readiness_check=lambda: next(readiness_checks, False),
            sleep_interval_seconds=0.0,
        )

        self.assertFalse(ready)

    def test_wait_for_backend_ready_returns_true_when_health_check_passes(self) -> None:
        process = _StubProcess([None])
        readiness_checks = iter([False, False, True])

        ready = _wait_for_backend_ready(
            timeout_seconds=1.0,
            backend_process=process,
            readiness_check=lambda: next(readiness_checks, True),
            sleep_interval_seconds=0.0,
        )

        self.assertTrue(ready)
