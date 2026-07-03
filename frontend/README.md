# Job Applier — Web Console (React + TypeScript)

A clean, minimal SPA front-end for the Job Applier Agent, built with
**Vite + React + TypeScript + Tailwind v4 + shadcn/ui**. It talks to the
FastAPI backend over JSON (`/api/*`, see [`backend/app/web/api.py`](../backend/app/web/api.py)).
The classic HTMX pages remain available and untouched — this is additive.

## Components (shadcn/ui)

UI primitives live in `src/components/ui/` and are owned by this repo (shadcn
copies source in, it is not a runtime dependency). Add more with:

```bash
npx shadcn@latest add <component>     # e.g. dialog, table, select, sonner
```

Theme tokens (colors, radius) are defined as CSS variables in `src/index.css`.
The Badge component has extra `success` / `warning` variants layered on top of
the stock shadcn set.

## Develop

Run the two processes side by side:

```bash
# 1) Backend (from backend/)
uvicorn app.main:app --reload            # http://127.0.0.1:8000

# 2) Frontend (from frontend/)
npm install
npm run dev                              # http://localhost:5173
```

The Vite dev server proxies `/api` to the backend on `:8000`, so open
<http://localhost:5173>.

## Production build

```bash
npm run build          # type-checks, then emits frontend/dist
```

When `frontend/dist` exists, the backend serves the built SPA at
**`/ui`** (see [`backend/app/main.py`](../backend/app/main.py)) — no Node process
needed at runtime. Open <http://127.0.0.1:8000/ui/>.

## Layout

- `src/lib/api.ts` — typed API client + response types
- `src/lib/hooks.ts` — `usePolling` fetch/poll hook
- `src/lib/utils.ts` — `cn()` class-merge helper; `src/lib/tone.ts` — status→Badge variant
- `src/components/ui/` — shadcn primitives (Button, Card, Badge, Alert, Skeleton, Separator)
- `src/components/Layout.tsx` — sidebar shell + `PageHeader`
- `src/index.css` — Tailwind entry + theme tokens
- `src/pages/` — `Dashboard`, `Jobs`, and `Setup` are live; the rest are
  placeholders pending migration.
