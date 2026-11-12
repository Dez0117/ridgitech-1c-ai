import asyncio
import logging
from uuid import UUID

from agent.models import AgentResult

from backend.models import Message, utc_now
from backend.protocols import AgentRunner, MessageCache, UnitOfWorkFactory


logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"completed", "needs_clarification", "failed"}


class AgentRunService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        cache: MessageCache,
        agent: AgentRunner,
    ):
        self.uow_factory = uow_factory
        self.cache = cache
        self.agent = agent

    async def execute(
        self,
        run_id: UUID,
        *,
        question: str,
        organization_id: str,
        history: str,
    ) -> None:
        async with self.uow_factory() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                logger.error("Agent run not found", extra={"run_id": str(run_id)})
                return
            if run.status in TERMINAL_STATUSES or run.assistant_message_id is not None:
                return
            run.status = "running"
            conversation_id = run.conversation_id
            await uow.commit()

        try:
            result = await self.agent.run(
                question,
                organization_id=organization_id,
                history=history,
            )
        except asyncio.CancelledError:
            raise
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

        async with self.uow_factory() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.status in TERMINAL_STATUSES or run.assistant_message_id is not None:
                return
            conversation = await uow.conversations.get(run.conversation_id)
            if conversation is None:
                logger.error("Conversation not found", extra={"run_id": str(run_id)})
                return

            assistant_message = Message(
                conversation_id=run.conversation_id,
                role="assistant",
                content=result.message,
                result=result.model_dump(mode="json"),
            )
            uow.messages.add(assistant_message)
            await uow.flush()
            run.assistant_message_id = assistant_message.id
            run.request_id = result.request_id
            run.status = result.status
            run.trace = result.trace
            run.error = result.error
            run.usage = result.usage
            run.cost_usd = result.cost_usd
            run.model_calls = result.model_calls
            run.elapsed_seconds = result.elapsed_seconds
            run.completed_at = utc_now()
            conversation.updated_at = utc_now()
            await uow.commit()

        try:
            await self.cache.invalidate(conversation_id)
        except Exception:
            logger.warning("Redis invalidation failed", exc_info=True)
