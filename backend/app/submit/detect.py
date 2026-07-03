"""Detect application forms that require a human (fail-safe before any auto-fill).

`classify_form` is a pure function over page HTML so it is fully unit-testable.
It returns a human-readable reason string when intervention is needed, else None.
Categories: captcha, video intro, free-text essay, (unsupported handled upstream).
"""
from __future__ import annotations

import re

_CAPTCHA = re.compile(
    r"(g-recaptcha|recaptcha|h-captcha|hcaptcha|cf-turnstile|turnstile|data-sitekey|captcha)",
    re.IGNORECASE,
)
_VIDEO = re.compile(
    r"(video\s+introduction|record\s+a\s+video|video\s+response|upload\s+a\s+video|"
    r"loom\.com|accept=[\"']?video|video/\*)",
    re.IGNORECASE,
)
_ESSAY_PHRASE = re.compile(
    r"(why\s+do\s+you\s+want\s+to\s+work|please\s+describe|in\s+your\s+own\s+words|"
    r"tell\s+us\s+about\s+a\s+time|write\s+an?\s+essay|\bessay\b)",
    re.IGNORECASE,
)
# A *required* textarea is a strong signal of a custom free-text question
# (standard cover-letter textareas are typically optional).
_REQUIRED_TEXTAREA = re.compile(r"<textarea[^>]*\brequired\b", re.IGNORECASE)


def classify_form(html: str) -> str | None:
    html = html or ""
    if _CAPTCHA.search(html):
        return "captcha"
    if _VIDEO.search(html):
        return "video introduction required"
    if _ESSAY_PHRASE.search(html) or _REQUIRED_TEXTAREA.search(html):
        return "free-text essay required"
    return None
