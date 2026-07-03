"""Runtime controls: dashboard-editable knobs layered over env Settings.

The pipeline functions accept a `Settings`; `effective_settings` returns a copy of
the env Settings with the persisted Control overrides applied, so dashboard edits
(kill switch, DRY_RUN, cap, threshold) take effect without a restart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Control


def get_or_create_control(session: Session) -> Control:
    control = session.exec(select(Control)).first()
    if control is None:
        s = get_settings()
        control = Control(
            dry_run=s.dry_run,
            submit_kill_switch=s.submit_kill_switch,
            daily_submit_cap=s.daily_submit_cap,
            match_threshold=s.match_threshold,
            cycle_interval_minutes=s.cycle_interval_minutes,
            scheduler_enabled=False,
        )
        session.add(control)
        session.commit()
        session.refresh(control)
    return control


def effective_settings(session: Session) -> Settings:
    base = get_settings()
    control = get_or_create_control(session)
    return base.model_copy(
        update={
            "dry_run": control.dry_run,
            "submit_kill_switch": control.submit_kill_switch,
            "daily_submit_cap": control.daily_submit_cap,
            "match_threshold": control.match_threshold,
            "cycle_interval_minutes": control.cycle_interval_minutes,
        }
    )


def update_control(session: Session, **fields) -> Control:
    control = get_or_create_control(session)
    for key, value in fields.items():
        setattr(control, key, value)
    control.updated_at = datetime.now(timezone.utc)
    session.add(control)
    session.commit()
    session.refresh(control)
    return control
