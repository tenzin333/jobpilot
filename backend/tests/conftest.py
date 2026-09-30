"""Shared test setup.

Several test modules point DATABASE_URL at a file-backed SQLite db (rather than
`:memory:`) because they exercise the FastAPI app through its real engine. Those
files survive the run, so rows leaked by a failed test — or by a test whose
cleanup never ran — would break every later run with a stale UNIQUE constraint.
Delete them before any test module is imported (and therefore before any engine
is created), so each session starts from an empty db.
"""
from __future__ import annotations

import os
from pathlib import Path

# The developer's .env intentionally enables the local ATS. Unit tests opt into
# it with explicit settings/fakes; unrelated submission tests stay live-mode.
os.environ["TEST_ATS_ENABLED"] = "false"
os.environ["SUBMIT_KILL_SWITCH"] = "false"

_BACKEND = Path(__file__).resolve().parent.parent

for _stale in _BACKEND.glob("test_*.db"):
    _stale.unlink(missing_ok=True)
