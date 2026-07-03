"""Phase 4 acceptance: anti-fabrication, real PDF render, tailoring orchestration."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.llm import tailoring  # noqa: E402
from app.llm.tailoring import RoleBullets, TailoredResume, verify_no_fabrication  # noqa: E402
from app.models import Application, ApplicationStatus, Job, Profile  # noqa: E402
from app.pipeline import tailor as tailor_pipeline  # noqa: E402
from app.resume.render import render_cover_letter_pdf, render_resume_pdf  # noqa: E402


def _profile() -> Profile:
    return Profile(
        full_name="Jane Engineer",
        email="jane@example.com",
        skills=["Python", "Kubernetes"],
        raw_text="Built distributed systems in Python and Go. Led platform team.",
        experience=[{"company": "Acme", "title": "Engineer", "start": "2020", "end": "2024", "bullets": []}],
    )


def test_verify_no_fabrication_flags_unknown_skill():
    profile = _profile()
    tailored = TailoredResume(highlighted_skills=["Python", "Rust"])  # Rust not in profile
    removed = verify_no_fabrication(tailored, profile)
    assert removed == ["Rust"]


def test_tailor_strips_fabricated_skills(monkeypatch):
    profile = _profile()
    job = Job(source="greenhouse", source_job_id="1", company="Beta", title="Backend Engineer",
              dedup_hash="h", ats_type="greenhouse", description="Python role")

    fake = TailoredResume(
        summary="Strong backend engineer.",
        highlighted_skills=["Python", "Kubernetes", "Rust"],  # Rust fabricated
        roles=[RoleBullets(company="Acme", title="Engineer", bullets=["Built systems in Python"])],
        cover_letter="Dear hiring manager...",
    )
    monkeypatch.setattr(tailoring, "parse_structured", lambda **kw: fake)

    result, removed = tailoring.tailor(job, profile)
    assert removed == ["Rust"]
    assert "Rust" not in result.highlighted_skills
    assert "Python" in result.highlighted_skills


def test_render_pdfs_real(tmp_path: Path):
    profile = _profile()
    job = Job(source="greenhouse", source_job_id="1", company="Beta", title="Backend Engineer",
              dedup_hash="h", ats_type="greenhouse", description="Python role")
    tailored = TailoredResume(
        summary="Strong backend engineer.",
        highlighted_skills=["Python", "Kubernetes"],
        roles=[RoleBullets(company="Acme", title="Engineer", bullets=["Built systems in Python"])],
        cover_letter="Dear hiring manager, I am excited to apply...",
    )

    resume_pdf = render_resume_pdf(tailored, profile, tmp_path / "resume.pdf")
    cover_pdf = render_cover_letter_pdf(tailored, job, profile, tmp_path / "cover.pdf")

    for pdf in (resume_pdf, cover_pdf):
        assert pdf.exists()
        assert pdf.read_bytes()[:4] == b"%PDF"


def test_tailor_ranked_orchestration(monkeypatch, tmp_path: Path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    profile = _profile()
    session.add(profile)
    job = Job(source="greenhouse", source_job_id="1", company="Beta", title="Backend Engineer",
              dedup_hash="h", ats_type="greenhouse", description="Python role")
    session.add(job)
    session.commit()
    # One above threshold, one below.
    session.add(Application(job_id=job.id, status=ApplicationStatus.ranked.value, match_score=85))
    low_job = Job(source="lever", source_job_id="2", company="Gamma", title="Sales",
                  dedup_hash="h2", ats_type="lever", description="")
    session.add(low_job)
    session.commit()
    session.add(Application(job_id=low_job.id, status=ApplicationStatus.ranked.value, match_score=40))
    session.commit()

    # Stub LLM + rendering for the orchestration test.
    monkeypatch.setattr(
        tailor_pipeline, "tailor",
        lambda job, profile: (TailoredResume(summary="ok", cover_letter="hi"), []),
    )
    monkeypatch.setattr(tailor_pipeline, "render_resume_pdf", lambda t, p, out: out.parent.mkdir(parents=True, exist_ok=True) or out.write_bytes(b"%PDF-1.4") or out)
    monkeypatch.setattr(tailor_pipeline, "render_cover_letter_pdf", lambda t, j, p, out: out.parent.mkdir(parents=True, exist_ok=True) or out.write_bytes(b"%PDF-1.4") or out)

    settings = Settings(match_threshold=70, data_dir=tmp_path)
    stats = tailor_pipeline.tailor_ranked(session, profile, settings)
    assert stats["tailored"] == 1

    apps = session.exec(select(Application).where(Application.status == ApplicationStatus.tailored.value)).all()
    assert len(apps) == 1
    assert apps[0].resume_path and apps[0].cover_letter_path
