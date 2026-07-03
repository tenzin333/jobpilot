"""Daily summary: aggregate the day's activity and (optionally) email it."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage

from sqlmodel import Session, func, select

from app.config import Settings
from app.models import Application, ApplicationStatus, Job, Run


@dataclass
class DailySummary:
    day: date
    discovered: int = 0
    ranked: int = 0
    tailored: int = 0
    submitted: int = 0
    needs_human_today: int = 0
    failed_today: int = 0
    needs_human_open: int = 0
    top_matches: list[tuple[Application, Job]] = field(default_factory=list)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def build_summary(session: Session, day: date | None = None, top_n: int = 10) -> DailySummary:
    day = day or datetime.now(timezone.utc).date()
    start, end = _day_bounds(day)

    runs = session.exec(select(Run).where(Run.started_at >= start, Run.started_at < end)).all()
    discovered = sum(r.discovered for r in runs)
    ranked = sum(r.ranked for r in runs)
    tailored = sum(r.tailored for r in runs)

    submitted = session.exec(
        select(func.count()).select_from(Application).where(
            Application.status == ApplicationStatus.submitted.value,
            Application.submitted_at >= start,
            Application.submitted_at < end,
        )
    ).one()
    needs_human_today = session.exec(
        select(func.count()).select_from(Application).where(
            Application.status == ApplicationStatus.needs_human.value,
            Application.updated_at >= start,
            Application.updated_at < end,
        )
    ).one()
    failed_today = session.exec(
        select(func.count()).select_from(Application).where(
            Application.status == ApplicationStatus.failed.value,
            Application.updated_at >= start,
            Application.updated_at < end,
        )
    ).one()
    needs_human_open = session.exec(
        select(func.count()).select_from(Application).where(
            Application.status == ApplicationStatus.needs_human.value
        )
    ).one()

    top_matches = session.exec(
        select(Application, Job).join(Job).order_by(Application.match_score.desc()).limit(top_n)
    ).all()

    return DailySummary(
        day=day,
        discovered=discovered,
        ranked=ranked,
        tailored=tailored,
        submitted=submitted,
        needs_human_today=needs_human_today,
        failed_today=failed_today,
        needs_human_open=needs_human_open,
        top_matches=list(top_matches),
    )


def render_summary_text(s: DailySummary) -> str:
    lines = [
        f"Job Applier Agent — daily summary for {s.day.isoformat()}",
        "",
        f"Discovered: {s.discovered}",
        f"Ranked:     {s.ranked}",
        f"Tailored:   {s.tailored}",
        f"Submitted:  {s.submitted}",
        f"Failed:     {s.failed_today}",
        f"Needs you (new today): {s.needs_human_today}",
        f"Needs you (open queue): {s.needs_human_open}",
        "",
        "Top matches:",
    ]
    if s.top_matches:
        for app, job in s.top_matches:
            score = app.match_score if app.match_score is not None else "—"
            lines.append(f"  [{score}] {job.company} — {job.title} ({app.status})")
    else:
        lines.append("  (none yet)")
    return "\n".join(lines)


def send_summary_email(settings: Settings, summary: DailySummary) -> bool:
    """Send the summary via SMTP. Returns False (no-op) if SMTP isn't configured."""
    if not (settings.smtp_host and settings.summary_email_to and settings.smtp_from):
        return False

    msg = EmailMessage()
    msg["Subject"] = f"Job Applier Agent — {summary.day.isoformat()} ({summary.submitted} submitted)"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.summary_email_to
    msg.set_content(render_summary_text(summary))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
    return True
