import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .api.routes import router
from .cache import ConversationCache, cache, get_cache
from .config import settings
from .database import engine, get_session
from .schemas import HealthResponse


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await cache.close()
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health(
    session: AsyncSession = Depends(get_session), conversation_cache: ConversationCache = Depends(get_cache)
):
    database_status = "ok"
    redis_status = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database_status = "error"
    try:
        await conversation_cache.ping()
    except Exception:
        redis_status = "error"
    overall = "ok" if database_status == redis_status == "ok" else "degraded"
    return HealthResponse(status=overall, database=database_status, redis=redis_status)
