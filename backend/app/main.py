"""FastAPI application: serves the JSON API and the React (Vite) SPA.

Startup initialises the DB. The JSON API lives under ``/api`` (consumed by the
React console at ``/ui``); ``/`` redirects to the SPA. The automatic scheduler is
intentionally disabled — runs happen only via explicit API actions.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Surface app INFO logs (setup, discovery, resume parsing) on the console.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.db import engine
    from app.pipeline.test_apply import Coordinator

    Coordinator(engine).recover()
    yield
    # Close the managed assist browser (if one was opened) on shutdown.
    from app.submit.assist_session import shutdown as assist_shutdown

    assist_shutdown()


app = FastAPI(title="Job Applier Agent", lifespan=lifespan)


class SPAStaticFiles(StaticFiles):
    """Serve the SPA entry point for client-side routes on a direct refresh."""

    async def get_response(self, path: str, scope):  # noqa: ANN001
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and not Path(path).suffix:
            return await super().get_response("index.html", scope)
        return response

# Serve the built React (Vite) SPA at /ui when it has been built (frontend/dist).
# In development the SPA runs from the Vite dev server (npm run dev), which proxies
# /api to this backend — so this mount is only used for production builds.
# __file__ is backend/app/main.py, so parents[2] is the repo root that holds frontend/.
_SPA_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _SPA_DIST.is_dir():
    app.mount("/ui", SPAStaticFiles(directory=str(_SPA_DIST), html=True), name="spa")

from app.web.api import router as api_router  # noqa: E402
from app.web.assist_ws import router as assist_ws_router  # noqa: E402
from app.web.test_executions import router as test_executions_router  # noqa: E402

app.include_router(api_router)
app.include_router(assist_ws_router)
app.include_router(test_executions_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    """The React SPA is the only UI; send the root there."""
    return RedirectResponse(url="/ui")
