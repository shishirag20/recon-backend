"""FastAPI application entrypoint.

Every feature lives in its own package under ``app`` and follows the same
layering: ``router`` (HTTP) -> ``service`` (business logic) -> ``dao`` (DB access),
with ``schema`` (Pydantic in/out) and ``constants`` shared across the module.

Run: ``uvicorn app.main:app --reload``
"""
from fastapi import FastAPI

from recon.app.auth.router import router as auth_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Reconciliation Platform API",
        version="0.1.0",
    )

    # Feature routers are registered here as modules are added.
    app.include_router(auth_router)

    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
