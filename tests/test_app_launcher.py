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

    def test_main_without_command_does_not_manage_flask_camera_bridge(self) -> None:
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

    def test_main_non_ui_command_runs_handler_without_flask_bridge_management(self) -> None:
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
                attendance_app.main()
            finally:
                sys.modules.pop("app", None)

        handler.assert_called_once()
