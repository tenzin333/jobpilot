"""Phase 0 acceptance: app boots, tables create, /health responds, / -> /ui."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_job_applier.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_health_and_root_redirect():
    with TestClient(app) as client:  # triggers lifespan -> init_db()
        assert client.get("/health").json() == {"status": "ok"}

        # The React SPA is the only UI; the root redirects to it.
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/ui"


def test_tables_created():
    from sqlalchemy import inspect

    from app.db import engine, init_db

    init_db()
    tables = set(inspect(engine).get_table_names())
    assert {"profile", "job", "application", "run"} <= tables
