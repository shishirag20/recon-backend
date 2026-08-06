"""FastAPI application entrypoint.

Every feature lives in its own package under ``app`` and follows the same
layering: ``router`` (HTTP) -> ``service`` (business logic) -> ``dao`` (DB access),
with ``schema`` (Pydantic in/out) and ``constants`` shared across the module.

Run: ``uvicorn app.main:app --reload``
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from recon.app.auth.router import router as auth_router
from recon.app.datahub.router import router as datahub_router
from recon.app.db.pool import create_pool


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
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Reconciliation Platform API",
        version="0.1.0",
        description="Backend for the reconciliation platform (auth, data ingestion, matching).",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    # Feature routers are registered here as modules are added.
    app.include_router(auth_router)
    app.include_router(datahub_router)

    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
