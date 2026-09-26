from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR
from .jobs import job_registry
from .jsonsafe import SafeJSONResponse
from .ratelimit import RateLimitMiddleware
from .routes import cleaning, export, jobs, outliers, project, sessions
from .sessions import session_manager

FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Any job persisted as queued/running when the process last exited is
    # marked interrupted - never presented as completed.
    job_registry.recover_interrupted()
    session_manager.start_cleanup_loop(interval=60)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quick Data Cleaner",
        version="0.1.0",
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )

    # CORS only needed for the Vite dev server; production serves same-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware)

    app.include_router(sessions.router)
    app.include_router(jobs.router)
    app.include_router(project.router)
    app.include_router(cleaning.router)
    app.include_router(export.router)
    app.include_router(outliers.router)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app


app = create_app()
