"""Shared helpers for search-aggregator connectors."""
from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(content: str) -> str:
    return _TAG_RE.sub(" ", html.unescape(content or "")).strip()


def matches_any(text: str, keywords: list[str]) -> bool:
    """True if any keyword appears in text (case-insensitive). Empty list -> True."""
    if not keywords:
        return True
    blob = (text or "").lower()
    return any(k.lower() in blob for k in keywords if k.strip())


def keyword_in_title(title: str, keywords: list[str]) -> bool:
    """True if any keyword matches as a WHOLE WORD/phrase in the title.

    Word-boundary matching avoids short tokens like "AI" matching inside words
    ("available", "maintain"). Empty keyword list -> True (keep all).
    """
    if not keywords:
        return True
    t = (title or "").lower()
    for k in keywords:
        k = k.strip().lower()
        if k and re.search(rf"\b{re.escape(k)}\b", t):
            return True
    return False


def relevant_title(title: str, include: list[str], exclude: list[str]) -> bool:
    """Keep a title if it matches an include keyword AND no exclude keyword.

    `include` empty -> include check passes. `exclude` empty -> nothing excluded.
    """
    if include and not keyword_in_title(title, include):
        return False
    if exclude and keyword_in_title(title, exclude):
        return False
    return True


def is_remote(*parts: str) -> bool:
    return "remote" in " ".join(parts).lower()
