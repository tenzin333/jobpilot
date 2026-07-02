"""Render tailored HTML to PDF using Playwright Chromium.

WeasyPrint was dropped (needs native GTK libs unavailable on this host); Chromium
is already a dependency for submission, so we reuse it for high-fidelity PDFs.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.llm.tailoring import TailoredResume
from app.models import Job, Profile

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _html_to_pdf(html: str, out_path: Path) -> Path:
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out_path), format="Letter", print_background=True)
        browser.close()
    return out_path


def render_resume_pdf(tailored: TailoredResume, profile: Profile, out_path: Path) -> Path:
    html = _env.get_template("resume.html").render(tailored=tailored, profile=profile)
    return _html_to_pdf(html, out_path)


def render_cover_letter_pdf(tailored: TailoredResume, job: Job, profile: Profile, out_path: Path) -> Path:
    html = _env.get_template("cover_letter.html").render(tailored=tailored, job=job, profile=profile)
    return _html_to_pdf(html, out_path)
