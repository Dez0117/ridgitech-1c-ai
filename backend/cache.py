import json
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from .config import settings


class ConversationCache:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def key(conversation_id: UUID) -> str:
        return f"conversation:{conversation_id}:messages"

    async def get_messages(self, conversation_id: UUID) -> list[dict[str, Any]] | None:
        value = await self.redis.get(self.key(conversation_id))
        return json.loads(value) if value else None

    async def set_messages(self, conversation_id: UUID, messages: list[dict[str, Any]]) -> None:
        await self.redis.set(
            self.key(conversation_id), json.dumps(messages, ensure_ascii=False, default=str), ex=self.ttl_seconds
        )

    async def invalidate(self, conversation_id: UUID) -> None:
        await self.redis.delete(self.key(conversation_id))

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()


redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
cache = ConversationCache(redis_client, settings.cache_ttl_seconds)


def get_cache() -> ConversationCache:
    return cache
