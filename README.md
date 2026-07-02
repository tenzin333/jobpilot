# Job Applier Agent

Autonomous AI job-application agent. From a base résumé + preferences it
continuously **discovers** jobs (Greenhouse, Lever, generic career pages),
**deduplicates** and **ranks** them against your profile, **tailors** a résumé +
cover letter for strong matches, and **auto-submits** on supported flows —
pausing for you only on essays, video intros, CAPTCHAs, or unsupported forms.
A local web dashboard handles setup, monitoring, intervention, and daily summaries.

## Stack
Python 3.11+ · FastAPI + HTMX dashboard · SQLite (SQLModel) · Playwright
(scraping, submission, PDF rendering) · Hugging Face Inference Providers
(`Qwen/Qwen2.5-72B-Instruct` for scoring/tailoring, `meta-llama/Llama-3.1-8B-Instruct`
for the relevance prefilter — both set in `.env`).

## Setup
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on POSIX)
pip install -e ".[dev]"
python -m playwright install chromium

copy .env.example .env                       # then edit: HF_TOKEN, SMTP, etc.
copy config\preferences.example.yaml config\preferences.yaml
```

## Run
```bash
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 and:
1. **Setup** — upload your résumé (PDF/DOCX) and set preferences + source slugs.
2. **Jobs** — *Discover now* to ingest postings.
3. **Applications** — *Rank now* → *Tailor above threshold* → *Submit tailored*.
4. **Intervention** — finish anything that needs you, then *Mark done*.
5. **Pipeline / Settings** — enable the scheduler and tune the controls.

## Safety
- **DRY_RUN** (default on): forms are filled but never finally submitted.
- **Kill switch**: blocks all submissions.
- **Per-source autonomy**: disable auto-submit per ATS.
- **Daily cap**: limits submissions/day. **CAPTCHAs are always routed to you.**

All four are editable live on the **Settings** page (override `.env` defaults).

## Tests
```bash
pytest -q
```
Submission tests run only against local `file://` HTML fixtures — never a real site.

## Job sources
- **Per-company ATS APIs:** Greenhouse, Lever, Ashby, Workable, SmartRecruiters
  (list company slugs in `preferences.yaml`). Ships with ~26 verified AI companies.
- **Keyword-search aggregators:** The Muse + Remotive (no key), Adzuna (free key) —
  these search your `desired_roles`, so results are relevant from the start.
- **Generic career pages:** opt-in Playwright scraper.

Discovery applies a title relevance gate: keep titles matching `desired_roles`,
drop titles matching `exclude_keywords` (e.g. seniority levels above your target).
Auto-submit currently covers Greenhouse + Lever; other sources route to the
intervention queue.

## Layout
`app/discovery` connectors · `app/pipeline` ingest/normalize/dedup/rank-tailor-submit/orchestrator
· `app/llm` Claude client + ranking + tailoring · `app/resume` parse + render
· `app/submit` adapters + intervention detection · `app/tracking` reporting · `app/web` dashboard.
