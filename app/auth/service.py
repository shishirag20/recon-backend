"""Business logic for auth.

The service orchestrates the dao and security helpers, enforces rules, and
raises HTTP-friendly errors. It takes a dao instance (dependency-injected by the
router) so it stays easy to test.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from recon.app.auth.constants import DEFAULT_ROLE, AuthErrors
from recon.app.auth.dao import AuthDAO
from recon.app.auth.schema import LoginRequest, RegisterRequest, TokenResponse, UserOut

# Shared security helpers live under app/core (hashing + JWT); added later.
# from app.core.security import hash_password, verify_password, create_token


class AuthService:
    def __init__(self, dao: AuthDAO) -> None:
        self.dao = dao

    async def register(self, payload: RegisterRequest) -> UserOut:
        if await self.dao.get_by_email(payload.email):
            raise HTTPException(status.HTTP_409_CONFLICT, AuthErrors.EMAIL_TAKEN)

        # hashed = hash_password(payload.password)
        # user = await self.dao.create(
        #     email=payload.email,
        #     hashed_password=hashed,
        #     full_name=payload.full_name,
        #     role=DEFAULT_ROLE,
        # )
        # return UserOut.model_validate(user)
        raise NotImplementedError

    async def login(self, payload: LoginRequest) -> TokenResponse:
        user = await self.dao.get_by_email(payload.email)
        # if not user or not verify_password(payload.password, user.hashed_password):
        #     raise HTTPException(status.HTTP_401_UNAUTHORIZED, AuthErrors.INVALID_CREDENTIALS)
        # if not user.is_active:
        #     raise HTTPException(status.HTTP_403_FORBIDDEN, AuthErrors.INACTIVE_USER)
        # return TokenResponse(
        #     access_token=create_token(user.id, ACCESS_TOKEN_TYPE),
        #     refresh_token=create_token(user.id, REFRESH_TOKEN_TYPE),
        # )
        raise NotImplementedError

    async def refresh(self, refresh_token: str) -> TokenResponse:
        # payload = decode_token(refresh_token)  -> validate type == refresh
        # user = await self.dao.get_by_id(payload.sub)
        # ...issue a fresh access token
        raise NotImplementedError
