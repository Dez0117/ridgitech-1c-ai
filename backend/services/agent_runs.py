import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.models import AgentResult

from backend.cache import ConversationCache, cache
from backend.database import SessionFactory
from backend.integrations.agent_service import AgentService, agent_service
from backend.models import AgentRun, Conversation, Message


logger = logging.getLogger(__name__)


class AgentRunProcessor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        conversation_cache: ConversationCache,
        agent: AgentService,
    ):
        self.session_factory = session_factory
        self.conversation_cache = conversation_cache
        self.agent = agent

    async def process(
        self,
        run_id: UUID,
        *,
        question: str,
        organization_id: str,
        history: str,
    ) -> None:
        try:
            result = await self.agent.run(
                question,
                organization_id=organization_id,
                history=history,
            )
        except Exception:
            logger.exception("Unexpected agent integration error", extra={"run_id": str(run_id)})
            result = AgentResult(
                status="failed",
                request_id=str(run_id),
                message="Сервис агента временно недоступен.",
                error={
                    "code": "AGENT_UNAVAILABLE",
                    "message": "Сервис агента временно недоступен.",
                },
            )

        async with self.session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                logger.error("Agent run disappeared before completion", extra={"run_id": str(run_id)})
                return
            if run.status != "running":
                logger.warning("Agent run is already completed", extra={"run_id": str(run_id)})
                return
            conversation = await session.get(Conversation, run.conversation_id)
            if conversation is None:
                logger.error("Conversation disappeared before completion", extra={"run_id": str(run_id)})
                return

            conversation_id = run.conversation_id
            assistant_message = Message(
                conversation_id=run.conversation_id,
                role="assistant",
                content=result.message,
                result=result.model_dump(mode="json"),
            )
            session.add(assistant_message)
            await session.flush()
            run.assistant_message_id = assistant_message.id
            run.request_id = result.request_id
            run.status = result.status
            run.trace = result.trace
            run.error = result.error
            run.usage = result.usage
            run.cost_usd = result.cost_usd
            run.model_calls = result.model_calls
            run.elapsed_seconds = result.elapsed_seconds
            run.completed_at = datetime.now(timezone.utc)
            conversation.updated_at = datetime.now(timezone.utc)
            await session.commit()

        try:
            await self.conversation_cache.invalidate(conversation_id)
        except Exception:
            logger.warning("Redis invalidation failed", exc_info=True)


agent_run_processor = AgentRunProcessor(SessionFactory, cache, agent_service)


def get_agent_run_processor() -> AgentRunProcessor:
    return agent_run_processor
