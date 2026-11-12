from fastapi import Depends

from backend.cache import ConversationCache, get_cache
from backend.config import settings
from backend.database import SessionFactory
from backend.protocols import AgentRunQueue
from backend.queue import get_agent_run_queue
from backend.repositories import SqlAlchemyUnitOfWork
from backend.services.chat import ChatService


def create_uow():
    return SqlAlchemyUnitOfWork(SessionFactory)


def get_chat_service(
    queue: AgentRunQueue = Depends(get_agent_run_queue),
    cache: ConversationCache = Depends(get_cache),
) -> ChatService:
    return ChatService(
        create_uow,
        queue,
        cache,
        history_messages=settings.agent_history_messages,
    )
