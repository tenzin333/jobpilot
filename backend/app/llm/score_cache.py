"""Persistent cache for LLM match scores.

A thin key/value layer over the ScoreCache table. Keys are opaque hashes built
by the caller (see app.llm.ranking) from everything that influences a score —
the score kind, the model, the profile, and the job text — so any change to those
misses the cache and recomputes. Values are the score's `model_dump()` dict.
"""
from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from app.models import ScoreCache


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_key(kind: str, model: str, profile_sig: str, job_sig: str) -> str:
    """A stable cache key. `kind` is 'prefilter' | 'deep'; `model` invalidates the
    entry when the user switches models; the two sigs cover profile and job text."""
    return sha(f"{kind}|{model}|{profile_sig}|{job_sig}")


def get_many(session: Session, keys: list[str]) -> dict[str, dict]:
    """Fetch cached payloads for `keys` (missing keys are simply absent)."""
    if not keys:
        return {}
    rows = session.exec(select(ScoreCache).where(ScoreCache.key.in_(keys))).all()
    return {r.key: r.payload for r in rows}


def put(session: Session, key: str, kind: str, payload: dict) -> None:
    """Store (or overwrite) one cached score. Commits immediately so a later
    rate-limited call in the same run can't lose already-cached results."""
    row = session.get(ScoreCache, key)
    if row is not None:
        row.payload = payload
        row.kind = kind
    else:
        session.add(ScoreCache(key=key, kind=kind, payload=payload))
    session.commit()
