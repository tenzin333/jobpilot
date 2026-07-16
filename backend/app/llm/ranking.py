"""Ranking: hard filters -> batched fast prefilter -> deep score.

Hard filters remove clear mismatches with no LLM cost. The prefilter scores
candidates 0-10 in BATCHES (many jobs per LLM call) to stay within free-tier
rate limits. The top N then get a structured deep score that gates
tailoring/submission downstream. Progress is reported to PIPELINE_STATE so the
UI can show it live, and deep scores commit per-job so cards fill in gradually.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.config import Preferences, RemotePreference, get_settings
from app.llm import score_cache
from app.llm.client import parse_structured
from app.models import Application, ApplicationStatus, Job, Profile
from app.pipeline.state import PIPELINE_STATE

# --- schemas -------------------------------------------------------------

class PrefilterScore(BaseModel):
    score: int = 0
    reason: str = ""


class ItemScore(BaseModel):
    ref: int
    score: int = 0


class BatchPrefilter(BaseModel):
    scores: list[ItemScore] = Field(default_factory=list)


class MatchScore(BaseModel):
    score: int = Field(ge=0, le=100, description="Match 0-100")
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)


class DeepItem(BaseModel):
    ref: int
    score: int = Field(default=0, ge=0, le=100)
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)


class BatchDeep(BaseModel):
    scores: list[DeepItem] = Field(default_factory=list)


# --- hard filters (no LLM) ----------------------------------------------

_NO_SPONSORSHIP = re.compile(
    r"(no\s+(visa\s+)?sponsorship|not?\s+(able|able to)\s+to\s+sponsor|without\s+sponsorship|"
    r"do(es)?\s+not\s+(provide|offer)\s+(visa\s+)?sponsorship|"
    r"must\s+be\s+(legally\s+)?authorized\s+to\s+work[^.]*without\s+sponsorship)",
    re.IGNORECASE,
)


def passes_location(job: Job, prefs: Preferences) -> bool:
    pref = prefs.remote_preference
    if job.remote:
        return pref in (RemotePreference.remote_only, RemotePreference.hybrid_ok, RemotePreference.any)
    if pref == RemotePreference.remote_only:
        return False
    # On-site/unknown job and the user is open to non-remote work. Only apply a
    # geographic gate if they named real cities — "Remote" in the list expresses
    # remote interest, not a location restriction, so on its own it must not
    # reject every on-site job (which would make `any` behave like remote_only).
    real_locations = [p for p in prefs.locations if p.strip().lower() != "remote"]
    if not real_locations:
        return True
    loc = (job.location or "").lower()
    return any(p.lower() in loc for p in real_locations)


def passes_salary(job: Job, prefs: Preferences) -> bool:
    if prefs.min_salary is None or job.salary_max is None:
        return True  # don't penalize unknown salary
    return job.salary_max >= prefs.min_salary


def passes_sponsorship(job: Job, prefs: Preferences) -> bool:
    if not prefs.require_sponsorship:
        return True
    return _NO_SPONSORSHIP.search(job.description or "") is None


def passes_hard_filters(job: Job, prefs: Preferences) -> bool:
    return passes_location(job, prefs) and passes_salary(job, prefs) and passes_sponsorship(job, prefs)


# --- profile prefix (stable, cached) ------------------------------------

def build_profile_prefix(profile: Profile) -> str:
    skills = ", ".join(profile.skills) if profile.skills else "(none parsed)"
    exp_lines = []
    for e in profile.experience:
        exp_lines.append(f"- {e.get('title','')} @ {e.get('company','')} ({e.get('start','')}-{e.get('end','')})")
    prefs = profile.preferences or {}
    return (
        "You are screening jobs for a candidate. Candidate profile:\n"
        f"Name: {profile.full_name}\n"
        f"Skills: {skills}\n"
        "Experience:\n" + ("\n".join(exp_lines) if exp_lines else "(none parsed)") + "\n"
        f"Desired roles: {', '.join(prefs.get('desired_roles', []))}\n"
        f"Preferred locations: {', '.join(prefs.get('locations', []))}\n"
        f"Remote preference: {prefs.get('remote_preference', 'any')}\n"
        "Resume (verbatim, do not invent beyond this):\n"
        f"{profile.raw_text[:6000]}"
    )


def _job_summary(job: Job) -> str:
    return (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
        f"Remote: {job.remote}\nDescription:\n{job.description[:4000]}"
    )


# --- LLM stages ---------------------------------------------------------

_PREFILTER_SYSTEM_SUFFIX = (
    "\n\nYou are screening a batch of jobs for this candidate. For EVERY job ref "
    "number, return a relevance score 0-10 (10 = strong fit to the candidate's "
    "skills/experience/desired roles). Return a score for every ref."
)


def _batch_brief(job: Job) -> str:
    return f"{job.title} @ {job.company} ({job.location})\n{job.description[:600]}"


def prefilter(
    jobs: list[Job], profile: Profile, group_size: int = 20
) -> dict[int, PrefilterScore]:
    """Score candidates 0-10 in batches of `group_size` per LLM call.

    Keyed by job.id. A failed batch is skipped (those jobs get no score). Reports
    progress to PIPELINE_STATE.
    """
    if not jobs:
        return {}
    system = build_profile_prefix(profile) + _PREFILTER_SYSTEM_SUFFIX
    groups = [jobs[i : i + group_size] for i in range(0, len(jobs), group_size)]
    result: dict[int, PrefilterScore] = {}
    done = 0

    for group in groups:
        refmap = {idx: job.id for idx, job in enumerate(group, start=1)}
        lines = [f"[{idx}] {_batch_brief(job)}" for idx, job in enumerate(group, start=1)]
        user = "Score each job below.\n\n" + "\n\n".join(lines)
        try:
            batch = parse_structured(
                system=system, user=user, schema=BatchPrefilter,
                tier="fast", cache_system=False, max_tokens=1000,
            )
            for item in batch.scores:
                jid = refmap.get(item.ref)
                if jid is not None:
                    result[jid] = PrefilterScore(score=max(0, min(10, item.score)))
        except Exception:  # noqa: BLE001 — skip a failed batch, keep going
            pass
        done += len(group)
        PIPELINE_STATE.update_stats(scored=done)
        PIPELINE_STATE.log(f"Prefiltered {done}/{len(jobs)} candidates")
    return result


_DEEP_SYSTEM_SUFFIX = (
    "\n\nScore how well this candidate matches the job from 0-100. Base the score "
    "ONLY on the candidate's real experience and skills above. List concrete gaps. "
    "Do not reward skills the candidate does not have."
)


def deep_score(job: Job, profile: Profile) -> MatchScore:
    system = build_profile_prefix(profile) + _DEEP_SYSTEM_SUFFIX
    return parse_structured(
        system=system,
        user=_job_summary(job),
        schema=MatchScore,
        tier="fast",  # 8B model: fast + higher rate limit (quality tier kept for tailoring)
        max_tokens=700,  # score + rationale + gaps; small output fits the 8B free-tier cap
        cache_system=True,
    )


_DEEP_BATCH_SYSTEM_SUFFIX = (
    "\n\nScore how well this candidate matches EACH job below from 0-100, using "
    "ONLY the candidate's real experience and skills above. For every job ref "
    "number return a score, a one-sentence rationale, and concrete gaps. Do not "
    "reward skills the candidate does not have. Return a result for every ref."
)


def _has_usable_description(job: Job, min_len: int = 80) -> bool:
    """Whether a job carries enough description text to deep-score meaningfully.

    Some sources (e.g. LinkedIn guest cards) return title-only postings with no
    body. Deep-scoring those just injects noise — the model has nothing beyond the
    title to judge — so they keep their (title-based) prefilter score instead.
    """
    return len((job.description or "").strip()) >= min_len


def _deep_brief(job: Job) -> str:
    return (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
        f"Remote: {job.remote}\nDescription:\n{job.description[:1500]}"
    )


def deep_score_batch(
    jobs: list[Job], profile: Profile, group_size: int = 8
) -> dict[int, MatchScore]:
    """Deep-score jobs 0-100 (+ rationale, gaps) in BATCHES of `group_size` per LLM
    call — one request scores many jobs instead of one-per-job. Keyed by job.id;
    a failed group is skipped (those jobs keep their prefilter score)."""
    if not jobs:
        return {}
    system = build_profile_prefix(profile) + _DEEP_BATCH_SYSTEM_SUFFIX
    groups = [jobs[i : i + group_size] for i in range(0, len(jobs), group_size)]
    out: dict[int, MatchScore] = {}

    for group in groups:
        refmap = {idx: job.id for idx, job in enumerate(group, start=1)}
        lines = [f"[{idx}] {_deep_brief(job)}" for idx, job in enumerate(group, start=1)]
        user = "Score each job below.\n\n" + "\n\n".join(lines)
        try:
            batch = parse_structured(
                # Deep scoring runs on the QUALITY tier (a stronger model): this is
                # the step whose score + rationale the user acts on, and it only
                # covers the top `deep_keep` jobs in small batches, so the extra
                # cost stays within free-tier limits. The prefilter stays on `fast`.
                system=system, user=user, schema=BatchDeep,
                tier="quality", cache_system=False, max_tokens=1500,
            )
        except Exception:  # noqa: BLE001 — skip a failed group, keep the prefilter score
            continue
        for item in batch.scores:
            jid = refmap.get(item.ref)
            if jid is not None:
                out[jid] = MatchScore(
                    score=max(0, min(100, item.score)),
                    rationale=item.rationale,
                    gaps=item.gaps,
                )
    return out


# --- caching layer ------------------------------------------------------
#
# Wrap the two LLM stages with a persistent cache so re-running (Discover or
# Re-score) only calls the model for jobs whose inputs actually changed. A job's
# cache key hashes its brief text; the profile signature hashes the full profile
# prefix; the model name is included so switching models recomputes.

def _profile_sig(profile: Profile) -> str:
    return score_cache.sha(build_profile_prefix(profile))


def _cached_prefilter(
    session: Session, jobs: list[Job], profile: Profile
) -> dict[int, PrefilterScore]:
    model = get_settings().resolve_tier("fast")[1]
    psig = _profile_sig(profile)
    keymap = {
        j.id: score_cache.make_key("prefilter", model, psig, score_cache.sha(_batch_brief(j)))
        for j in jobs
    }
    hits = score_cache.get_many(session, list(keymap.values()))
    out: dict[int, PrefilterScore] = {}
    misses: list[Job] = []
    for j in jobs:
        payload = hits.get(keymap[j.id])
        if payload is not None:
            out[j.id] = PrefilterScore(**payload)
        else:
            misses.append(j)
    if misses:
        fresh = prefilter(misses, profile)
        for j in misses:
            score = fresh.get(j.id)
            if score is not None:
                out[j.id] = score
                score_cache.put(session, keymap[j.id], "prefilter", score.model_dump())
    return out


def _cached_deep_score_batch(
    session: Session, jobs: list[Job], profile: Profile
) -> dict[int, MatchScore]:
    model = get_settings().resolve_tier("quality")[1]
    psig = _profile_sig(profile)
    keymap = {
        j.id: score_cache.make_key("deep", model, psig, score_cache.sha(_deep_brief(j)))
        for j in jobs
    }
    hits = score_cache.get_many(session, list(keymap.values()))
    out: dict[int, MatchScore] = {}
    misses: list[Job] = []
    for j in jobs:
        payload = hits.get(keymap[j.id])
        if payload is not None:
            out[j.id] = MatchScore(**payload)
        else:
            misses.append(j)
    if misses:
        fresh = deep_score_batch(misses, profile)
        for jid, score in fresh.items():
            out[jid] = score
            score_cache.put(session, keymap[jid], "deep", score.model_dump())
    return out


# --- orchestration ------------------------------------------------------

def rank_jobs(
    session: Session,
    profile: Profile,
    prefs: Preferences,
    prefilter_keep: int = 25,
    prefilter_min: int = 5,
    prefilter_max: int = 80,
    deep_keep: int = 25,
) -> dict[str, int]:
    """(Re)rank jobs that are new or still in the `ranked` state. Returns counts.

    hard filter -> cap to newest `prefilter_max` -> batched prefilter -> persist
    a match score from the prefilter for every survivor (so cards always show a
    score even when the deep model is rate-limited) -> upgrade the top `deep_keep`
    with a deep score + rationale (best-effort).

    Re-scorable = jobs with no Application, or one still in `ranked` (so "Re-score"
    can refresh a job that was rate-limited into a poor/zero score). Jobs whose
    Application has advanced (tailored/queued/submitted/needs_human) or failed are
    left untouched — those are in-flight, done, or handled by the retry flow.
    """
    existing = {a.job_id: a for a in session.exec(select(Application)).all()}
    # Jobs locked by an Application that must not be re-scored.
    locked = {jid for jid, a in existing.items() if a.status != ApplicationStatus.ranked.value}
    all_jobs = session.exec(select(Job)).all()
    candidates = [
        j for j in all_jobs
        if j.id not in locked and passes_hard_filters(j, prefs)
    ]
    filtered_out = len(all_jobs) - len(locked) - len(candidates)

    if not candidates:
        return {"candidates": 0, "filtered_out": max(filtered_out, 0), "scored": 0, "ranked": 0}

    # Bound LLM volume: prefilter only the most recently discovered candidates.
    to_score = sorted(candidates, key=lambda j: j.discovered_at, reverse=True)[:prefilter_max]

    pre = _cached_prefilter(session, to_score, profile)
    survivors = sorted(
        (j for j in to_score if pre.get(j.id, PrefilterScore(score=0)).score >= prefilter_min),
        key=lambda j: pre[j.id].score,
        reverse=True,
    )[:prefilter_keep]

    # 1. Persist a score for every survivor from the (fast, batched) prefilter,
    #    reusing an existing `ranked` Application when re-scoring.
    apps: dict[int, Application] = {}
    ranked = 0
    for job in survivors:
        pscore = pre[job.id].score
        will_deep = _has_usable_description(job)
        app = existing.get(job.id) or Application(job_id=job.id)
        app.status = ApplicationStatus.ranked.value
        app.match_score = pscore * 10  # 0-10 -> 0-100
        app.score_rationale = (
            f"Prefilter relevance {pscore}/10 (awaiting detailed review)."
            if will_deep
            else f"Prefilter relevance {pscore}/10 - no job description available for a detailed review."
        )
        app.gaps = []  # cleared; a deep score below re-populates when available
        session.add(app)
        session.commit()
        apps[job.id] = app
        ranked += 1
        PIPELINE_STATE.update_stats(ranked=ranked)

    # 2. Upgrade the top few with a deep score + rationale, BATCHED (a handful of
    #    LLM calls instead of one-per-job) to stay well within free-tier request
    #    limits. Skip description-less jobs (nothing to deep-score) and let them
    #    keep the prefilter score. Best-effort: a failed group leaves it in place.
    to_deep = [j for j in survivors[:deep_keep] if _has_usable_description(j)]
    if to_deep:
        for job_id, score in _cached_deep_score_batch(session, to_deep, profile).items():
            app = apps[job_id]
            app.match_score = score.score
            app.score_rationale = score.rationale
            app.gaps = score.gaps
            session.add(app)
            session.commit()
            PIPELINE_STATE.log(f"Deep-scored job {job_id} -> {score.score}")

    return {
        "candidates": len(candidates),
        "filtered_out": max(filtered_out, 0),
        "scored": len(to_score),
        "ranked": ranked,
    }
