"""FastAPI application entrypoint.

Every feature lives in its own package under ``app`` and follows the same
layering: ``router`` (HTTP) -> ``service`` (business logic) -> ``dao`` (DB access),
with ``schema`` (Pydantic in/out) and ``constants`` shared across the module.

Run: ``uvicorn app.main:app --reload``
"""

import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.router import router as auth_router
from app.datahub.router import router as datahub_router
from app.reconciliation.router import router as reconciliation_router
from app.db.pool import create_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    try:
        yield
    finally:
        await app.state.pool.close()


OPENAPI_TAGS = [
    {
        "name": "Data Hub",
        "description": (
            "Ingest external files into the platform: register a data source, define how its "
            "columns map onto canonical fields, upload files against it, and review/promote the "
            "results. Uploads are asynchronous - `POST /ingestion-jobs` returns immediately with "
            "`status=PENDING`; a background worker does the actual parsing and you poll "
            "`GET /ingestion-jobs/{job_id}` for the outcome."
        ),
    },
    {
        "name": "Reconciliation",
        "description": (
            "AR reconciliation: define a rule catalog for an entity, enqueue a run over bank "
            "statements + sub-ledger invoices, and (once the engine milestones land) review "
            "matches/exceptions and sign off. `POST .../runs` currently only enqueues "
            "(`status=QUEUED`) - see app/reconciliation/router.py's milestone map for what's "
            "implemented today vs. pending the reconciliation worker."
        ),
    },
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Reconciliation Platform API",
        version="0.1.0",
        description="Backend for the reconciliation platform (auth, data ingestion, matching).",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    # CORS middleware with environment variable configuration
    raw_origins = os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173",
    )
    allowed_origins = [
        origin.strip() for origin in raw_origins.split(",") if origin.strip()
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    api_v1_router = APIRouter(prefix="/api/v1")
    api_v1_router.include_router(auth_router)
    api_v1_router.include_router(datahub_router)
    api_v1_router.include_router(reconciliation_router)

    app.include_router(api_v1_router)

    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
