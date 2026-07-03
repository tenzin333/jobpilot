"""Phase 0 acceptance: app boots, tables create, /health and dashboard respond."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_job_applier.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_health_and_dashboard():
    with TestClient(app) as client:  # triggers lifespan -> init_db()
        assert client.get("/health").json() == {"status": "ok"}

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text


def test_tables_created():
    from sqlalchemy import inspect

    from app.db import engine, init_db

    init_db()
    tables = set(inspect(engine).get_table_names())
    assert {"profile", "job", "application", "run"} <= tables
