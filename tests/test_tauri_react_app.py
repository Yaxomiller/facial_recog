from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.tauri_react_app import NativeShellUnavailable, find_tauri_binary, launch_tauri_react_app


class TauriReactAppTests(unittest.TestCase):
    def test_find_tauri_binary_uses_env_override(self) -> None:
        configured_path = Path("C:/tmp/tresenso-face-attendance")
        with patch.dict(os.environ, {"ATTENDANCE_TAURI_BINARY": str(configured_path)}, clear=False):
            with patch("src.tauri_react_app._is_launchable_binary", side_effect=lambda candidate: candidate == configured_path):
                self.assertEqual(find_tauri_binary(), configured_path)

    def test_find_tauri_binary_uses_release_build_when_present(self) -> None:
        expected_path = Path("release/tresenso-face-attendance")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATTENDANCE_TAURI_BINARY", None)
            with patch("src.tauri_react_app.TAURI_TARGET_DIR", Path(".")):
                with patch("src.tauri_react_app._tauri_binary_filename", return_value="tresenso-face-attendance"):
                    with patch(
                        "src.tauri_react_app._is_launchable_binary",
                        side_effect=lambda candidate: candidate == expected_path,
                    ):
                        self.assertEqual(find_tauri_binary(), expected_path)

    def test_launch_tauri_react_app_requires_built_binary(self) -> None:
        with patch("src.tauri_react_app.find_tauri_binary", return_value=None):
            with self.assertRaisesRegex(NativeShellUnavailable, "prebuilt Tauri app"):
                launch_tauri_react_app()

    def test_launch_tauri_react_app_runs_binary(self) -> None:
        binary_path = Path("/tmp/tresenso-face-attendance")
        with patch("src.tauri_react_app.find_tauri_binary", return_value=binary_path):
            with patch("src.tauri_react_app.subprocess.run") as subprocess_run:
                launch_tauri_react_app()

        subprocess_run.assert_called_once_with(
            [str(binary_path)],
            cwd=binary_path.parent,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
