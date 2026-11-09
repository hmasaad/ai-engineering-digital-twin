"""Paths and tunables shared by the scanner, model, and server."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "northwind"
FRONTEND = ROOT / "frontend"
DEFAULT_DB = ROOT / "backend" / "data" / "twin.sqlite"

CHURN_WINDOW_DAYS = 120
HISTORY_WINDOW_DAYS = 180
HALF_LIFE_DAYS = 60
MAX_FILES = 4000
MAX_FILE_BYTES = 500_000
LEARNING_RATE = 0.55
MONTE_CARLO_TRIALS = 400
MONTE_CARLO_SEED = 7


def database_path() -> Path:
    override = os.environ.get("TWIN_DB")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_DB
