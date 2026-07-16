"""Shared test setup.

Several test modules point DATABASE_URL at a file-backed SQLite db (rather than
`:memory:`) because they exercise the FastAPI app through its real engine. Those
files survive the run, so rows leaked by a failed test — or by a test whose
cleanup never ran — would break every later run with a stale UNIQUE constraint.
Delete them before any test module is imported (and therefore before any engine
is created), so each session starts from an empty db.
"""
from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent

for _stale in _BACKEND.glob("test_*.db"):
    _stale.unlink(missing_ok=True)
