# Job Applier Agent

Autonomous AI job-application agent. From a base résumé + preferences it
continuously **discovers** jobs (Greenhouse, Lever, generic career pages),
**deduplicates** and **ranks** them against your profile, **tailors** a résumé +
cover letter for strong matches, and **auto-submits** on supported flows —
pausing for you only on essays, video intros, CAPTCHAs, or unsupported forms.
A local web dashboard handles setup, monitoring, intervention, and daily summaries.

## Structure
This is a monorepo with the Python backend and React frontend separated:
```
backend/    FastAPI API + pipeline (app/, tests/, config/, data/, pyproject.toml, .env)
frontend/   React + TypeScript + Vite + shadcn/ui SPA (see frontend/README.md)
.venv/      shared Python virtualenv (repo root)
```
The backend runs from `backend/`; the SPA talks to it over `/api` and is served at
`/ui` in production. The legacy HTMX dashboard still lives at `/`.

## Stack
Python 3.11+ · FastAPI (HTMX dashboard + JSON API) · React + TypeScript + Vite +
shadcn/ui · SQLModel over SQLite **or** managed Postgres (Neon) · Playwright
(scraping, submission, PDF rendering) · Groq / Hugging Face LLM backends (set in `.env`).

## Setup
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on POSIX)
pip install -e "./backend[dev]"
python -m playwright install chromium

copy backend\.env.example backend\.env                       # then edit: keys, DATABASE_URL, SMTP
copy backend\config\preferences.example.yaml backend\config\preferences.yaml
```

## Run
```bash
cd backend
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 (classic dashboard) or the React console at
http://127.0.0.1:8000/ui once the frontend is built. Then:
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
cd backend
pytest -q
```
Tests pin `DATABASE_URL` to a local SQLite file, so they never touch your configured
Postgres. Submission tests run only against local `file://` HTML fixtures — never a real site.

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

## Backend layout
`backend/app/discovery` connectors · `backend/app/pipeline` ingest/normalize/dedup/rank-tailor-submit/orchestrator
· `backend/app/llm` LLM client + ranking + tailoring · `backend/app/resume` parse + render
· `backend/app/submit` adapters + intervention detection · `backend/app/tracking` reporting
· `backend/app/web` HTMX dashboard + JSON API (`api.py`).
