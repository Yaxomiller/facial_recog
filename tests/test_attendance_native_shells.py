from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.attendance import _launch_preferred_native_shell
from src.native_react_app import NativeShellUnavailable


class AttendanceNativeShellSelectionTests(unittest.TestCase):
    def test_linux_prefers_tauri_first(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATTENDANCE_NATIVE_SHELL", None)
            with patch("src.attendance.sys.platform", "linux"):
                with patch("src.attendance.launch_tauri_react_app") as launch_tauri_react_app:
                    with patch("src.attendance.launch_native_react_app") as launch_native_react_app:
                        _launch_preferred_native_shell()

        launch_tauri_react_app.assert_called_once_with()
        launch_native_react_app.assert_not_called()

    def test_linux_falls_back_to_pywebview_when_tauri_is_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATTENDANCE_NATIVE_SHELL", None)
            with patch("src.attendance.sys.platform", "linux"):
                with patch(
                    "src.attendance.launch_tauri_react_app",
                    side_effect=NativeShellUnavailable("tauri missing"),
                ) as launch_tauri_react_app:
                    with patch("src.attendance.launch_native_react_app") as launch_native_react_app:
                        _launch_preferred_native_shell()

        launch_tauri_react_app.assert_called_once_with()
        launch_native_react_app.assert_called_once_with()

    def test_native_shell_env_override_can_force_pywebview_first(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_NATIVE_SHELL": "pywebview"}, clear=False):
            with patch("src.attendance.sys.platform", "linux"):
                with patch("src.attendance.launch_native_react_app") as launch_native_react_app:
                    with patch("src.attendance.launch_tauri_react_app") as launch_tauri_react_app:
                        _launch_preferred_native_shell()

        launch_native_react_app.assert_called_once_with()
        launch_tauri_react_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
