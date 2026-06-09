from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.attendance import _launch_native_with_browser_fallback
from src.native_react_app import NativeShellUnavailable


class AttendanceNativeFallbackTests(unittest.TestCase):
    def test_native_launcher_falls_back_to_browser_when_native_shell_is_unavailable(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_NATIVE_BROWSER_FALLBACK": "true"}, clear=False):
            with patch("src.attendance.launch_native_react_app", side_effect=NativeShellUnavailable("gtk missing")):
                with patch("src.attendance.launch_web_app") as launch_web_app:
                    _launch_native_with_browser_fallback()

        launch_web_app.assert_called_once_with(browser_mode="app")

    def test_native_launcher_can_disable_browser_fallback(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_NATIVE_BROWSER_FALLBACK": "false"}, clear=False):
            with patch("src.attendance.launch_native_react_app", side_effect=NativeShellUnavailable("gtk missing")):
                with patch("src.attendance.launch_web_app") as launch_web_app:
                    with self.assertRaises(NativeShellUnavailable):
                        _launch_native_with_browser_fallback()

        launch_web_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
