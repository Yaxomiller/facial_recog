from __future__ import annotations

import os


DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 8000


def get_web_host() -> str:
    return os.getenv("ATTENDANCE_WEB_HOST", DEFAULT_WEB_HOST).strip() or DEFAULT_WEB_HOST


def get_web_port() -> int:
    raw_port = os.getenv("ATTENDANCE_WEB_PORT", str(DEFAULT_WEB_PORT)).strip() or str(DEFAULT_WEB_PORT)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("ATTENDANCE_WEB_PORT must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("ATTENDANCE_WEB_PORT must be between 1 and 65535.")
    return port


def get_browser_host(server_host: str) -> str:
    if server_host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return server_host
