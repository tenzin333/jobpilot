"""Phase 1 acceptance: resume text extraction + setup round-trip.

Uses a real PDF fixture (generated with fpdf2) so the pdfplumber extraction
path is genuinely exercised. Claude is not called here (no API key in CI);
parse_resume falls back to raw-text-only extraction, which is what we assert.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_phase1.db")

from fastapi.testclient import TestClient  # noqa: E402
from fpdf import FPDF  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.config import Preferences  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Profile  # noqa: E402
from app.resume.parse import extract_text  # noqa: E402


def _make_pdf(path: Path, lines: list[str]) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for line in lines:
        pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def test_extract_text_pdf(tmp_path: Path):
    p = tmp_path / "resume.pdf"
    _make_pdf(p, ["Jane Engineer", "Senior Backend Engineer", "Python, Go, Kubernetes"])
    text = extract_text(p)
    assert "Jane Engineer" in text
    assert "Kubernetes" in text


def test_extract_text_unsupported(tmp_path: Path):
    p = tmp_path / "resume.txt"
    p.write_text("hello")
    with pytest.raises(ValueError):
        extract_text(p)


def test_setup_roundtrip_pdf(tmp_path: Path, monkeypatch):
    # Don't hit a real LLM during the test — force the raw-text fallback path.
    import app.resume.parse as parse_mod
    monkeypatch.setattr(parse_mod, "structured_extract", lambda raw: (_ for _ in ()).throw(RuntimeError("no llm")))

    # Isolate preferences.yaml to a temp file across all access points.
    prefs_path = tmp_path / "preferences.yaml"
    monkeypatch.setattr("app.config.PREFERENCES_PATH", prefs_path)
    monkeypatch.setattr("app.web.setup.get_preferences", lambda: Preferences.load(prefs_path))

    orig_save = Preferences.save
    monkeypatch.setattr(Preferences, "save", lambda self, path=prefs_path: orig_save(self, path))

    resume = tmp_path / "me.pdf"
    _make_pdf(resume, ["Alex Dev", "Platform Engineer", "Terraform, AWS"])

    with TestClient(app) as client:
        with resume.open("rb") as fh:
            resp = client.post(
                "/setup",
                data={
                    "desired_roles": "Backend Engineer\nPlatform Engineer",
                    "locations": "Remote\nNew York, NY",
                    "remote_preference": "hybrid_ok",
                    "min_salary": "150000",
                    "salary_currency": "USD",
                    "work_authorization": "US Citizen",
                    "greenhouse_companies": "stripe\nairbnb",
                    "lever_companies": "netflix",
                },
                files={"resume": ("me.pdf", fh, "application/pdf")},
                follow_redirects=False,
            )
    assert resp.status_code == 303

    # Preferences persisted to YAML.
    saved = Preferences.load(prefs_path)
    assert "Platform Engineer" in saved.desired_roles
    assert saved.min_salary == 150000
    assert saved.sources["greenhouse"].companies == ["stripe", "airbnb"]

    # Profile created with raw text from the uploaded PDF resume.
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
    assert profile is not None
    assert "Alex Dev" in profile.raw_text
    assert profile.preferences["min_salary"] == 150000
