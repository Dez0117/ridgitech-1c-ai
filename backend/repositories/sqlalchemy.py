from __future__ import annotations

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import AgentRun, Conversation, Message


class SqlAlchemyConversationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, conversation_id: UUID) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    def add(self, conversation: Conversation) -> None:
        self.session.add(conversation)


class SqlAlchemyMessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add(self, message: Message) -> None:
        self.session.add(message)

    async def list(self, conversation_id: UUID) -> list[Message]:
        result = await self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(result)

    async def recent(self, conversation_id: UUID, limit: int) -> list[Message]:
        if limit <= 0:
            return []
        result = await self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(list(result)))


class SqlAlchemyAgentRunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add(self, run: AgentRun) -> None:
        self.session.add(run)

    async def get(self, run_id: UUID) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def list(self, conversation_id: UUID) -> list[AgentRun]:
        result = await self.session.scalars(
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at, AgentRun.id)
        )
        return list(result)

    async def active(self, conversation_id: UUID) -> AgentRun | None:
        return await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.conversation_id == conversation_id,
                AgentRun.status.in_(("queued", "running")),
            )
            .limit(1)
        )


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self.session_factory()
        self.conversations = SqlAlchemyConversationRepository(self.session)
        self.messages = SqlAlchemyMessageRepository(self.session)
        self.runs = SqlAlchemyAgentRunRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        if exc is not None:
            await self.session.rollback()
        await self.session.close()

    async def flush(self) -> None:
        assert self.session is not None
        await self.session.flush()

    async def commit(self) -> None:
        assert self.session is not None
        await self.session.commit()

    async def rollback(self) -> None:
        assert self.session is not None
        await self.session.rollback()
