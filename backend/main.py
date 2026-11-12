import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .api.routes import router
from .cache import ConversationCache, cache, get_cache
from .config import settings
from .database import engine, get_session
from .exceptions import QueueUnavailableError, ResourceNotFoundError, RunInProgressError
from .protocols import AgentRunQueue
from .queue import agent_run_queue, get_agent_run_queue
from .schemas import HealthResponse


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(*, manage_resources: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_resources:
            await agent_run_queue.start()
        yield
        if manage_resources:
            await agent_run_queue.close()
            await cache.close()
            await engine.dispose()

    application = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)

    @application.exception_handler(ResourceNotFoundError)
    async def resource_not_found(_, exc: ResourceNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @application.exception_handler(RunInProgressError)
    async def run_in_progress(_, exc: RunInProgressError):
        location = f"/api/v1/agent-runs/{exc.run_id}"
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": "RUN_IN_PROGRESS", "run_id": str(exc.run_id)}},
            headers={"Location": location},
        )

    @application.exception_handler(QueueUnavailableError)
    async def queue_unavailable(_, exc: QueueUnavailableError):
        location = f"/api/v1/agent-runs/{exc.run_id}"
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "QUEUE_UNAVAILABLE", "run_id": str(exc.run_id)}},
            headers={"Location": location, "Retry-After": "5"},
        )

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health(
        session: AsyncSession = Depends(get_session),
        conversation_cache: ConversationCache = Depends(get_cache),
        queue: AgentRunQueue = Depends(get_agent_run_queue),
    ):
        database_status = "ok"
        redis_status = "ok"
        worker_status = "ok"
        try:
            await session.execute(text("SELECT 1"))
        except Exception:
            database_status = "error"
        try:
            await conversation_cache.ping()
        except Exception:
            redis_status = "error"
        try:
            if not await queue.worker_alive():
                worker_status = "error"
        except Exception:
            worker_status = "error"
        overall = "ok" if database_status == redis_status == worker_status == "ok" else "degraded"
        return HealthResponse(
            status=overall,
            database=database_status,
            redis=redis_status,
            worker=worker_status,
        )

    return application


app = create_app()
