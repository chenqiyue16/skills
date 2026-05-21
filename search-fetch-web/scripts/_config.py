"""Shared configuration for search-fetch scripts. All paths derive from SEARCH_FETCH_DATA_DIR."""

import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# Override via env var; default to skill-local .data/
DATA_DIR = Path(os.environ.get("SEARCH_FETCH_DATA_DIR", SKILL_DIR / ".data"))

STATE_PATH = DATA_DIR / "search-fetch-state.json"
RESEARCH_RUNS_DIR = DATA_DIR / "research-runs"
COOKIE_DIR = DATA_DIR / "cookies"
BILIBILI_COOKIE_PATH = COOKIE_DIR / "bilibili.txt"
TMP_DIR = DATA_DIR / "tmp"


def require_macos() -> None:
    """Raise if not running on macOS (used by Safari-based scripts)."""
    if sys.platform != "darwin":
        raise RuntimeError(f"Safari-based fetch requires macOS; current platform: {sys.platform}")
