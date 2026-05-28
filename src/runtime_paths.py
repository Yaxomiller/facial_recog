from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    configured = os.getenv("ATTENDANCE_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return BASE_DIR / "data"


DATA_DIR = get_data_dir()
