from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.config import settings


WORKER_FUNCTION = "execute_agent_run"
QUEUE_NAME = "arq:queue"
WORKER_HEALTH_KEY = f"{QUEUE_NAME}:health-check"


class ArqAgentRunQueue:
    def __init__(self, redis_url: str):
        self.redis_settings = RedisSettings.from_dsn(redis_url)
        self.redis: ArqRedis | None = None

    async def start(self) -> None:
        if self.redis is None:
            self.redis = await create_pool(self.redis_settings, default_queue_name=QUEUE_NAME)

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None

    async def enqueue(
        self,
        run_id: UUID,
        *,
        question: str,
        organization_id: str,
        history: str,
    ) -> str:
        await self.start()
        assert self.redis is not None
        job_id = str(run_id)
        job = await self.redis.enqueue_job(
            WORKER_FUNCTION,
            job_id,
            question,
            organization_id,
            history,
            _job_id=job_id,
            _queue_name=QUEUE_NAME,
        )
        return job.job_id if job is not None else job_id

    async def worker_alive(self) -> bool:
        await self.start()
        assert self.redis is not None
        return bool(await self.redis.exists(WORKER_HEALTH_KEY))


agent_run_queue = ArqAgentRunQueue(settings.redis_url)


def get_agent_run_queue() -> ArqAgentRunQueue:
    return agent_run_queue
