from __future__ import annotations

import argparse
import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


class AppLauncherTests(unittest.TestCase):
    def test_main_without_command_launches_browser_default(self) -> None:
        fake_attendance = types.ModuleType("src.attendance")
        parser = Mock()
        parser.parse_args.return_value = argparse.Namespace(command=None)
        fake_attendance.build_parser = Mock(return_value=parser)
        fake_attendance.launch_default_app = Mock()

        with patch.dict(sys.modules, {"src.attendance": fake_attendance}):
            sys.modules.pop("app", None)
            attendance_app = importlib.import_module("app")
            try:
                attendance_app.main()
            finally:
                sys.modules.pop("app", None)

        fake_attendance.launch_default_app.assert_called_once_with()

    def test_main_without_command_autostarts_and_stops_flask_camera_bridge(self) -> None:
        fake_attendance = types.ModuleType("src.attendance")
        parser = Mock()
        parser.parse_args.return_value = argparse.Namespace(command=None)
        fake_attendance.build_parser = Mock(return_value=parser)
        fake_attendance.launch_default_app = Mock()

        with patch.dict(sys.modules, {"src.attendance": fake_attendance}):
            sys.modules.pop("app", None)
            attendance_app = importlib.import_module("app")
            try:
                bridge_process = object()
                with (
                    patch.object(attendance_app, "_should_autostart_flask_camera_bridge", return_value=True),
                    patch.object(attendance_app, "_maybe_start_flask_camera_bridge", return_value=bridge_process) as start_bridge,
                    patch.object(attendance_app, "_stop_flask_camera_bridge") as stop_bridge,
                ):
                    attendance_app.main()
            finally:
                sys.modules.pop("app", None)

        start_bridge.assert_called_once_with()
        stop_bridge.assert_called_once_with(bridge_process)

    def test_main_non_ui_command_skips_flask_camera_bridge(self) -> None:
        fake_attendance = types.ModuleType("src.attendance")
        parser = Mock()
        handler = Mock()
        parser.parse_args.return_value = argparse.Namespace(command="train", handler=handler)
        fake_attendance.build_parser = Mock(return_value=parser)
        fake_attendance.launch_default_app = Mock()

        with patch.dict(sys.modules, {"src.attendance": fake_attendance}):
            sys.modules.pop("app", None)
            attendance_app = importlib.import_module("app")
            try:
                with (
                    patch.object(attendance_app, "_should_autostart_flask_camera_bridge", return_value=False),
                    patch.object(attendance_app, "_maybe_start_flask_camera_bridge") as start_bridge,
                    patch.object(attendance_app, "_stop_flask_camera_bridge") as stop_bridge,
                ):
                    attendance_app.main()
            finally:
                sys.modules.pop("app", None)

        handler.assert_called_once()
        start_bridge.assert_not_called()
        stop_bridge.assert_called_once_with(None)
