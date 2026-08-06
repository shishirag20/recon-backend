"""Pydantic request/response models for the auth module.

These are the API contract (validation + serialization). They are deliberately
separate from the ORM models (which live under ``app/db`` and are touched only
by the dao layer).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ── Requests ──────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Responses ─────────────────────────────────────────────────────────────
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: str
    is_active: bool

    # allow building this straight from an ORM object
    model_config = {"from_attributes": True}


# ── Internal (decoded JWT payload) ────────────────────────────────────────
class TokenPayload(BaseModel):
    sub: str          # user id
    type: str         # access | refresh
    exp: datetime | None = None
