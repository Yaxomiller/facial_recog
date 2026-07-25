from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import src.api_v2 as api_v2
import src.attendance as attendance


class DemoModeAuthTests(unittest.TestCase):
    def test_demo_mode_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": ""}, clear=False):
            self.assertFalse(api_v2.demo_mode_enabled())

    def test_auth_is_still_required_when_demo_mode_is_off(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": ""}, clear=False):
            with self.assertRaises(HTTPException) as raised:
                api_v2.require_auth(authorization=None, x_auth_token=None)
            self.assertEqual(raised.exception.status_code, 401)

            with self.assertRaises(HTTPException) as camera_raised:
                api_v2.require_camera_auth(token=None, authorization=None, x_auth_token=None)
            self.assertEqual(camera_raised.exception.status_code, 401)

    def test_invalid_token_is_still_rejected_when_demo_mode_is_off(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": ""}, clear=False):
            with patch.object(api_v2.session_store, "get_session", return_value=None):
                with self.assertRaises(HTTPException) as raised:
                    api_v2.require_auth(authorization="Bearer not-a-real-token", x_auth_token=None)
        self.assertEqual(raised.exception.status_code, 401)

    def test_demo_mode_grants_a_session_without_credentials(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": "1"}, clear=False):
            self.assertTrue(api_v2.demo_mode_enabled())
            session = api_v2.require_auth(authorization=None, x_auth_token=None)
            camera_session = api_v2.require_camera_auth(token=None, authorization=None, x_auth_token=None)

        self.assertEqual(session.username, "demo")
        self.assertEqual(camera_session.username, "demo")

    def test_demo_mode_accepts_common_truthy_values_only(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": value}, clear=False):
                self.assertTrue(api_v2.demo_mode_enabled(), value)
        for value in ("", "0", "false", "no", "off"):
            with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": value}, clear=False):
                self.assertFalse(api_v2.demo_mode_enabled(), value)


class DemoFrontendRouteTests(unittest.TestCase):
    def test_demo_route_serves_the_static_page(self) -> None:
        response = api_v2.demo_frontend()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.path), str(api_v2.DEMO_FRONTEND_INDEX))

    def test_demo_page_covers_every_required_feature(self) -> None:
        content = api_v2.DEMO_FRONTEND_INDEX.read_text(encoding="utf-8")

        # The four home options the demo must expose.
        for target in ('data-go="enroll"', 'data-go="scan"', 'data-go="database"', 'data-go="history"'):
            self.assertIn(target, content)

        # Attendance + breath flow wired to the real backend endpoints.
        for endpoint in (
            "/api/v2/workers/enroll",
            "/api/v2/workers",
            "/api/v2/recognitions",
            "/api/v2/detections",
            "/api/v2/breath-tests/start",
            "/api/v2/breath-tests/complete",
            "/api/v2/attendance",
            "stream.mjpg",
        ):
            self.assertIn(endpoint, content)


class DemoLauncherTests(unittest.TestCase):
    def test_parser_exposes_the_demo_mode(self) -> None:
        parser = attendance.build_parser()
        args = parser.parse_args(["demo"])
        self.assertIs(args.handler, attendance.handle_demo)

    def test_demo_mode_enables_the_bypass_and_opens_the_demo_page(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": ""}, clear=False):
            with patch.object(attendance, "launch_web_app") as launch:
                attendance.handle_demo(argparse.Namespace())
                self.assertEqual(os.environ["ATTENDANCE_DEMO_MODE"], "1")
        launch.assert_called_once_with(browser_mode="app", landing_path="/demo")

    def test_other_launch_modes_never_enable_demo_mode(self) -> None:
        for handler, expected in (
            (attendance.handle_kiosk, {"browser_mode": "app"}),
            (attendance.handle_simple, {"browser_mode": "app", "landing_path": "/simple"}),
            (attendance.handle_web, {"browser_mode": "web"}),
        ):
            with patch.dict(os.environ, {"ATTENDANCE_DEMO_MODE": ""}, clear=False):
                with patch.object(attendance, "launch_web_app") as launch:
                    handler(argparse.Namespace())
                    self.assertEqual(os.environ.get("ATTENDANCE_DEMO_MODE", ""), "")
            launch.assert_called_once_with(**expected)


if __name__ == "__main__":
    unittest.main()
