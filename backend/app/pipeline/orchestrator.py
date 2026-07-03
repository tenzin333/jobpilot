"""The continuous cycle: discover -> rank -> tailor -> submit, recording a Run.

Each stage is independently idempotent (dedup at discovery, status guards at
rank/tailor/submit), so cycles are safe to repeat and resume.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from app.config import get_preferences
from app.controls import effective_settings
from app.db import engine
from app.llm.ranking import rank_jobs
from app.models import Profile, Run
from app.pipeline.ingest import discover_and_store
from app.pipeline.state import PIPELINE_STATE
from app.pipeline.submit import submit_tailored
from app.pipeline.tailor import tailor_ranked


async def run_cycle(session: Session) -> Run:
    """Run one full pipeline cycle, reporting progress to PIPELINE_STATE."""
    prefs = get_preferences()
    settings = effective_settings(session)
    run = Run()
    session.add(run)
    session.commit()
    session.refresh(run)

    profile = session.exec(select(Profile)).first()

    # 1. Discover.
    PIPELINE_STATE.set_stage("Discovering jobs…")
    disc = await discover_and_store(session, prefs)
    run.discovered = disc["discovered"]
    run.deduped = disc["deduped"]
    PIPELINE_STATE.update_stats(discovered=disc["discovered"], deduped=disc["deduped"])
    PIPELINE_STATE.log(f"Discovered {disc['discovered']} new, {disc['deduped']} duplicates skipped")

    if profile is not None:
        # 2. Rank.
        PIPELINE_STATE.set_stage("Ranking (LLM)…")
        ranked = rank_jobs(session, profile, prefs)
        run.ranked = ranked["ranked"]
        PIPELINE_STATE.update_stats(ranked=ranked["ranked"], scored=ranked.get("scored", 0))
        PIPELINE_STATE.log(f"Scored {ranked.get('scored', 0)} candidates, ranked {ranked['ranked']}")

        # 3. Tailor (above threshold).
        PIPELINE_STATE.set_stage("Tailoring résumés (LLM)…")
        tailored = tailor_ranked(session, profile, settings)
        run.tailored = tailored["tailored"]
        PIPELINE_STATE.update_stats(tailored=tailored["tailored"])
        PIPELINE_STATE.log(f"Tailored {tailored['tailored']} applications")

        # 4. Submit.
        PIPELINE_STATE.set_stage("Submitting applications…")
        sub = submit_tailored(session, profile, prefs, settings)
        run.submitted = sub["submitted"]
        run.needs_human = sub["needs_human"]
        run.failed = sub["failed"]
        run.notes = (
            f"dry_run={settings.dry_run} dry_filled={sub['dry_run']} "
            f"skipped_cap={sub['skipped_cap']} candidates={ranked['candidates']}"
        )
        PIPELINE_STATE.update_stats(
            submitted=sub["submitted"], needs_human=sub["needs_human"], failed=sub["failed"]
        )
        PIPELINE_STATE.log(
            f"Submitted {sub['submitted']}, needs-human {sub['needs_human']}, "
            f"failed {sub['failed']} (dry_run={settings.dry_run})"
        )
    else:
        run.notes = "no profile configured; discovery only"
        PIPELINE_STATE.log("No profile configured — discovery only. Upload a résumé in Setup.")

    run.finished_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


async def run_cycle_standalone() -> Run:
    """Entry point for the scheduler (manages its own session)."""
    with Session(engine) as session:
        return await run_cycle(session)


async def discover_and_rank_standalone() -> None:
    """Discover jobs and score them (no tailoring/submission).

    Used by the Discover page so jobs arrive already carrying a match score,
    Tsenta-style. Discovery commits jobs first (they appear immediately), then
    ranking fills in scores.
    """
    with Session(engine) as session:
        prefs = get_preferences()
        PIPELINE_STATE.set_stage("Discovering jobs…")
        disc = await discover_and_store(session, prefs)
        PIPELINE_STATE.update_stats(discovered=disc["discovered"], deduped=disc["deduped"])
        PIPELINE_STATE.log(f"Discovered {disc['discovered']} new, {disc['deduped']} duplicates skipped")

        profile = session.exec(select(Profile)).first()
        if profile is None:
            PIPELINE_STATE.log("No profile configured — scores need a résumé (Setup).")
            return
        PIPELINE_STATE.set_stage("Scoring matches (LLM)…")
        ranked = rank_jobs(session, profile, prefs)
        PIPELINE_STATE.update_stats(ranked=ranked["ranked"], scored=ranked.get("scored", 0))
        PIPELINE_STATE.log(f"Scored {ranked.get('scored', 0)} candidates, ranked {ranked['ranked']}")


def _run_in_background(coro_factory) -> bool:
    """Run an async entrypoint in a daemon thread, tracked by PIPELINE_STATE.

    Returns False if a run is already in progress. Keeps the web server
    responsive during the run.
    """
    import asyncio
    import threading

    if not PIPELINE_STATE.start():
        return False

    def _run() -> None:
        try:
            asyncio.run(coro_factory())
            PIPELINE_STATE.finish()
        except Exception as exc:  # noqa: BLE001
            PIPELINE_STATE.finish(error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return True


def start_pipeline_run() -> bool:
    """Full cycle (discover → rank → tailor → submit) in the background."""
    return _run_in_background(run_cycle_standalone)


def start_discover_and_rank() -> bool:
    """Discover + score only, in the background."""
    return _run_in_background(discover_and_rank_standalone)
