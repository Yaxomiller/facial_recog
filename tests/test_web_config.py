from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.web_config import get_browser_host, get_web_host, get_web_port


class WebConfigTests(unittest.TestCase):
    def test_defaults_bind_on_all_interfaces(self) -> None:
        with patch.dict(
            os.environ,
            {"ATTENDANCE_WEB_HOST": "", "ATTENDANCE_WEB_PORT": ""},
            clear=False,
        ):
            self.assertEqual(get_web_host(), "0.0.0.0")
            self.assertEqual(get_web_port(), 8000)

    def test_browser_host_uses_loopback_for_wildcard_bind(self) -> None:
        self.assertEqual(get_browser_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(get_browser_host("::"), "127.0.0.1")
        self.assertEqual(get_browser_host("192.168.1.20"), "192.168.1.20")

    def test_invalid_port_raises_clear_error(self) -> None:
        with patch.dict(os.environ, {"ATTENDANCE_WEB_PORT": "abc"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "ATTENDANCE_WEB_PORT must be an integer."):
                get_web_port()

        with patch.dict(os.environ, {"ATTENDANCE_WEB_PORT": "70000"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "ATTENDANCE_WEB_PORT must be between 1 and 65535."):
                get_web_port()
