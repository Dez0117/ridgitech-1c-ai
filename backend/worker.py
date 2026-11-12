from typing import Any
from uuid import UUID

from arq.connections import RedisSettings

from backend.cache import cache
from backend.config import settings
from backend.database import SessionFactory, engine
from backend.integrations.agent_service import agent_service
from backend.queue import QUEUE_NAME
from backend.repositories import SqlAlchemyUnitOfWork
from backend.services.agent_runs import AgentRunService


def create_uow():
    return SqlAlchemyUnitOfWork(SessionFactory)


async def startup(ctx: dict[str, Any]) -> None:
    ctx["agent_run_service"] = AgentRunService(create_uow, cache, agent_service)


async def shutdown(ctx: dict[str, Any]) -> None:
    await cache.close()
    await engine.dispose()


async def execute_agent_run(
    ctx: dict[str, Any],
    run_id: str,
    question: str,
    organization_id: str,
    history: str,
) -> None:
    service: AgentRunService = ctx["agent_run_service"]
    await service.execute(
        UUID(run_id),
        question=question,
        organization_id=organization_id,
        history=history,
    )


class WorkerSettings:
    functions = [execute_agent_run]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = QUEUE_NAME
    max_jobs = 4
    max_tries = 3
    job_timeout = settings.agent_timeout_seconds + 30
    keep_result = 3600
    health_check_interval = 15
