"""Deduplication: stable exact hash + fuzzy near-duplicate detection."""
from __future__ import annotations

import hashlib
import re

from rapidfuzz import fuzz

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

# Two postings of the same company are near-dupes if their normalized
# "title @ location" strings score above this (catches reposts / minor edits).
FUZZY_THRESHOLD = 92


def normalize_text(value: str) -> str:
    value = (value or "").lower().strip()
    value = _PUNCT.sub(" ", value)
    return _WS.sub(" ", value).strip()


def dedup_hash(company: str, title: str, location: str) -> str:
    key = "|".join(normalize_text(v) for v in (company, title, location))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _signature(company: str, title: str, location: str) -> str:
    return f"{normalize_text(title)} @ {normalize_text(location)}"


def is_near_duplicate(
    company: str,
    title: str,
    location: str,
    existing_same_company: list[tuple[str, str]],
) -> bool:
    """True if (title, location) closely matches an existing posting at the same company.

    `existing_same_company` is a list of (title, location) tuples already stored
    for this company.
    """
    candidate = _signature(company, title, location)
    for ex_title, ex_location in existing_same_company:
        score = fuzz.token_sort_ratio(candidate, _signature(company, ex_title, ex_location))
        if score >= FUZZY_THRESHOLD:
            return True
    return False
