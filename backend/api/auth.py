import secrets
from datetime import timedelta
from typing import Annotated
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.config import settings
from backend.database import get_session
from backend.models import AuthSession, User, utc_now
from backend.schemas import LoginRequest, RegisterRequest, UserRead


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
COOKIE_NAME = "ridgitech_session"
ISSUER = "ridgitech-1c-ai"
SIGNING_KEY = settings.jwt_secret.get_secret_value() or secrets.token_urlsafe(48)
SECURE_COOKIE = settings.environment != "development"
hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))
Database = Annotated[AsyncSession, Depends(get_session)]
SessionToken = Annotated[str | None, Cookie(alias=COOKIE_NAME)]


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Войдите в аккаунт.")


def session_ids(token: str | None) -> tuple[UUID, UUID]:
    if not token:
        raise unauthorized()
    try:
        claims = jwt.decode(
            token,
            SIGNING_KEY,
            algorithms=["HS256"],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "jti"]},
        )
        return UUID(claims["sub"]), UUID(claims["jti"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise unauthorized() from exc


async def current_user(db: Database, token: SessionToken = None) -> User:
    user_id, session_id = session_ids(token)
    user = await db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(User.id == user_id, AuthSession.id == session_id, AuthSession.expires_at > utc_now())
    )
    if user is None:
        raise unauthorized()
    return user


async def start_session(db: AsyncSession, user: User, response: Response) -> None:
    now = utc_now()
    lifetime = timedelta(minutes=settings.jwt_session_minutes)
    session_id = uuid4()
    await db.flush()
    await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    db.add(AuthSession(id=session_id, user_id=user.id, expires_at=now + lifetime))
    await db.commit()
    token = jwt.encode(
        {"sub": str(user.id), "jti": str(session_id), "iss": ISSUER, "iat": now, "exp": now + lifetime},
        SIGNING_KEY,
        algorithm="HS256",
    )
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(lifetime.total_seconds()),
        secure=SECURE_COOKIE, httponly=True, samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: Database, response: Response):
    password_hash = await run_in_threadpool(hasher.hash, payload.password)
    user = User(email=payload.email, name=payload.name, password_hash=password_hash)
    db.add(user)
    try:
        await start_session(db, user, response)
    except IntegrityError:
        await db.rollback()
        if await db.scalar(select(User.id).where(User.email == payload.email)):
            raise HTTPException(status_code=409, detail="Этот адрес уже зарегистрирован.")
        raise
    return user


@router.post("/login", response_model=UserRead)
async def login(payload: LoginRequest, db: Database, response: Response):
    user = await db.scalar(select(User).where(User.email == payload.email))
    try:
        await run_in_threadpool(hasher.verify, user.password_hash if user else DUMMY_HASH, payload.password)
    except VerificationError:
        user = None
    if user is None:
        raise HTTPException(status_code=401, detail="Неверная почта или пароль.")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(hasher.hash, payload.password)
    await start_session(db, user, response)
    return user


@router.get("/me", response_model=UserRead)
async def me(response: Response, user: Annotated[User, Depends(current_user)]):
    response.headers["Cache-Control"] = "no-store"
    return user


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def logout(db: Database, response: Response, token: SessionToken = None):
    try:
        user_id, session_id = session_ids(token)
    except HTTPException:
        pass
    else:
        await db.execute(delete(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user_id))
        await db.commit()
    response.delete_cookie(COOKIE_NAME, secure=SECURE_COOKIE, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
