"""HTTP layer for auth — endpoints only.

The router validates input via ``schema``, delegates all work to ``AuthService``,
and returns ``schema`` responses. No business logic or DB access lives here.

Dependency wiring (``get_auth_service`` / ``get_current_user``) builds a service
from the request-scoped DB session. The shared ``get_session`` dependency will
move to ``app/db/session.py`` with the persistence layer; a placeholder is kept
here for now so the module is self-contained.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import ROUTER_PREFIX, ROUTER_TAGS
from app.auth.dao import AuthDAO
from app.auth.schema import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.auth.service import AuthService

router = APIRouter(prefix=ROUTER_PREFIX, tags=ROUTER_TAGS)


# ── Dependencies ──────────────────────────────────────────────────────────
async def get_session() -> AsyncSession:  # TODO: move to app/db/session.py
    """Yield a request-scoped DB session (shared infra placeholder)."""
    raise NotImplementedError


def get_auth_service(session: AsyncSession = Depends(get_session)) -> AuthService:
    return AuthService(AuthDAO(session))


async def get_current_user(
    service: AuthService = Depends(get_auth_service),
    # token: str = Depends(oauth2_scheme),   # from app/core/security
) -> UserOut:
    """Resolve the authenticated user from the bearer token."""
    raise NotImplementedError


# ── Endpoints ─────────────────────────────────────────────────────────────
@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
) -> UserOut:
    return await service.register(payload)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    return await service.login(payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    return await service.refresh(payload.refresh_token)


@router.get("/me", response_model=UserOut)
async def me(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    return current_user
