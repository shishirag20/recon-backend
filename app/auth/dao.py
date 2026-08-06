"""Data Access Object for auth.

The DAO is the *only* layer that talks to the database. It exposes plain,
intention-revealing methods (``get_by_email``, ``create``) and hides all query
details, so the service layer never sees SQLAlchemy.

The shared ``User`` ORM model and session live under ``app/db`` (added with the
persistence layer); imports are shown commented until then.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# from app.db.models import User


class AuthDAO:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str):
        """Return the user with this email, or ``None``."""
        # stmt = select(User).where(User.email == email)
        # result = await self.session.execute(stmt)
        # return result.scalar_one_or_none()
        raise NotImplementedError

    async def get_by_id(self, user_id: str):
        """Return the user with this id, or ``None``."""
        # return await self.session.get(User, user_id)
        raise NotImplementedError

    async def create(self, *, email: str, hashed_password: str, full_name: str, role: str):
        """Insert and return a new user."""
        # user = User(
        #     email=email,
        #     hashed_password=hashed_password,
        #     full_name=full_name,
        #     role=role,
        # )
        # self.session.add(user)
        # await self.session.flush()   # populate PK without committing the tx
        # return user
        raise NotImplementedError
