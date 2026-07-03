from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

import src.api_v2 as api_v2
import src.attendance as attendance


class SimpleFrontendRouteTests(unittest.TestCase):
    def test_simple_route_serves_the_static_terminal_page(self) -> None:
        response = api_v2.simple_frontend()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.path), str(api_v2.SIMPLE_FRONTEND_INDEX))
        self.assertEqual(response.headers["Cache-Control"], "no-store, no-cache, must-revalidate")

    def test_simple_frontend_file_exists_in_repo(self) -> None:
        self.assertTrue(api_v2.SIMPLE_FRONTEND_INDEX.exists())
        content = api_v2.SIMPLE_FRONTEND_INDEX.read_text(encoding="utf-8")
        self.assertIn("/api/v2/recognitions", content)
        self.assertIn("/api/v2/breath-tests/start", content)
        self.assertIn("stream.mjpg", content)


class SimpleLauncherTests(unittest.TestCase):
    def test_parser_exposes_the_simple_mode(self) -> None:
        parser = attendance.build_parser()
        args = parser.parse_args(["simple"])
        self.assertIs(args.handler, attendance.handle_simple)

    def test_simple_mode_opens_the_lightweight_terminal(self) -> None:
        with patch.object(attendance, "launch_web_app") as launch:
            attendance.handle_simple(argparse.Namespace())
        launch.assert_called_once_with(browser_mode="app", landing_path="/simple")

    def test_kiosk_mode_is_untouched_by_the_simple_mode(self) -> None:
        with patch.object(attendance, "launch_web_app") as launch:
            attendance.handle_kiosk(argparse.Namespace())
        launch.assert_called_once_with(browser_mode="app")


if __name__ == "__main__":
    unittest.main()
