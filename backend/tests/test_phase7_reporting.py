"""Phase 7 acceptance: summary aggregation, text render, SMTP send (mocked)."""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import Application, ApplicationStatus, Job, Run  # noqa: E402
from app.tracking import reporting  # noqa: E402
from app.tracking.reporting import build_summary, render_summary_text, send_summary_email  # noqa: E402


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session) -> None:
    now = datetime.now(timezone.utc)
    run = Run(discovered=10, deduped=3, ranked=5, tailored=2, submitted=1)
    session.add(run)
    job = Job(source="greenhouse", source_job_id="1", company="Acme", title="Engineer",
              dedup_hash="h", ats_type="greenhouse")
    session.add(job)
    session.commit()
    session.add(Application(job_id=job.id, status=ApplicationStatus.submitted.value,
                            match_score=90, submitted_at=now))
    job2 = Job(source="lever", source_job_id="2", company="Beta", title="SRE",
               dedup_hash="h2", ats_type="lever")
    session.add(job2)
    session.commit()
    session.add(Application(job_id=job2.id, status=ApplicationStatus.needs_human.value,
                            match_score=80, needs_human_reason="captcha", updated_at=now))
    session.commit()


def test_build_summary_counts():
    session = _session()
    _seed(session)
    s = build_summary(session)
    assert s.discovered == 10
    assert s.ranked == 5
    assert s.tailored == 2
    assert s.submitted == 1
    assert s.needs_human_open == 1
    assert s.needs_human_today == 1
    assert len(s.top_matches) == 2
    # Sorted by score descending.
    assert s.top_matches[0][0].match_score == 90


def test_render_summary_text():
    session = _session()
    _seed(session)
    text = render_summary_text(build_summary(session))
    assert "daily summary" in text
    assert "Submitted:  1" in text
    assert "Acme — Engineer" in text


def test_send_email_noop_when_unconfigured():
    session = _session()
    _seed(session)
    settings = Settings(smtp_host="", summary_email_to="", smtp_from="")
    assert send_summary_email(settings, build_summary(session)) is False


def test_send_email_configured(monkeypatch):
    session = _session()
    _seed(session)

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]

    monkeypatch.setattr(reporting.smtplib, "SMTP", FakeSMTP)
    settings = Settings(
        smtp_host="smtp.example.com", smtp_port=587, smtp_username="u", smtp_password="p",
        smtp_from="bot@example.com", summary_email_to="me@example.com",
    )
    assert send_summary_email(settings, build_summary(session)) is True
    assert sent["host"] == "smtp.example.com"
    assert sent["to"] == "me@example.com"
    assert "submitted" in sent["subject"].lower()
