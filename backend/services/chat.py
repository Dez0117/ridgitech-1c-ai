import logging
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from backend.exceptions import QueueUnavailableError, ResourceNotFoundError, RunInProgressError
from backend.models import AgentRun, Conversation, Message, utc_now
from backend.protocols import AgentRunQueue, MessageCache, UnitOfWorkFactory


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        queue: AgentRunQueue,
        cache: MessageCache,
        *,
        history_messages: int,
    ):
        self.uow_factory = uow_factory
        self.queue = queue
        self.cache = cache
        self.history_messages = history_messages

    @staticmethod
    def history_text(messages: list[Message]) -> str:
        history = "\n".join(f"{message.role}: {message.content}" for message in messages)
        return history[-20_000:]

    async def create_conversation(self, organization_id: str, title: str | None) -> Conversation:
        async with self.uow_factory() as uow:
            conversation = Conversation(organization_id=organization_id, title=title)
            uow.conversations.add(conversation)
            await uow.commit()
            return conversation

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        async with self.uow_factory() as uow:
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise ResourceNotFoundError("Conversation")
            return conversation

    async def list_messages(self, conversation_id: UUID) -> list[Message] | list[dict]:
        await self.get_conversation(conversation_id)
        try:
            cached = await self.cache.get_messages(conversation_id)
            if cached is not None:
                return cached
        except Exception:
            logger.warning("Redis read failed", exc_info=True)

        async with self.uow_factory() as uow:
            messages = await uow.messages.list(conversation_id)
        try:
            await self.cache.set_messages(
                conversation_id,
                [
                    {
                        "id": str(message.id),
                        "conversation_id": str(message.conversation_id),
                        "role": message.role,
                        "content": message.content,
                        "result": message.result,
                        "created_at": message.created_at.isoformat(),
                    }
                    for message in messages
                ],
            )
        except Exception:
            logger.warning("Redis write failed", exc_info=True)
        return messages

    async def get_run(self, run_id: UUID) -> AgentRun:
        async with self.uow_factory() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise ResourceNotFoundError("Agent run")
            return run

    async def list_runs(self, conversation_id: UUID) -> list[AgentRun]:
        async with self.uow_factory() as uow:
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise ResourceNotFoundError("Conversation")
            return await uow.runs.list(conversation_id)

    async def submit_message(self, conversation_id: UUID, content: str) -> tuple[Conversation, Message, AgentRun]:
        async with self.uow_factory() as uow:
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise ResourceNotFoundError("Conversation")
            active_run = await uow.runs.active(conversation_id)
            if active_run is not None:
                raise RunInProgressError(active_run.id)
            previous_messages = await uow.messages.recent(conversation_id, self.history_messages)
            history = self.history_text(previous_messages)

            user_message = Message(conversation_id=conversation_id, role="user", content=content)
            uow.messages.add(user_message)
            await uow.flush()
            run = AgentRun(conversation_id=conversation_id, user_message_id=user_message.id, status="queued")
            uow.runs.add(run)
            if conversation.title is None:
                conversation.title = content[:255]
            try:
                await uow.commit()
            except IntegrityError:
                await uow.rollback()
                active_run = await uow.runs.active(conversation_id)
                if active_run is not None:
                    raise RunInProgressError(active_run.id)
                raise

        await self._invalidate(conversation_id)
        try:
            await self.queue.enqueue(
                run.id,
                question=content,
                organization_id=conversation.organization_id,
                history=history,
            )
        except Exception:
            logger.exception("Failed to enqueue agent run", extra={"run_id": str(run.id)})
            await self._mark_queue_failure(run.id)
            raise QueueUnavailableError(run.id)
        return conversation, user_message, run

    async def _mark_queue_failure(self, run_id: UUID) -> None:
        async with self.uow_factory() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.status != "queued":
                return
            run.status = "failed"
            run.request_id = str(run.id)
            run.error = {"code": "QUEUE_UNAVAILABLE", "message": "Очередь задач временно недоступна."}
            run.completed_at = utc_now()
            await uow.commit()

    async def _invalidate(self, conversation_id: UUID) -> None:
        try:
            await self.cache.invalidate(conversation_id)
        except Exception:
            logger.warning("Redis invalidation failed", exc_info=True)
