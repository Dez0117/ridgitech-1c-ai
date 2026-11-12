from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from backend.models import AgentRun, Conversation, Message


if TYPE_CHECKING:
    from agent.models import AgentResult


class AgentRunner(Protocol):
    async def run(self, question: str, *, organization_id: str, history: str) -> AgentResult: ...


class AgentRunQueue(Protocol):
    async def enqueue(
        self,
        run_id: UUID,
        *,
        question: str,
        organization_id: str,
        history: str,
    ) -> str: ...

    async def worker_alive(self) -> bool: ...


class MessageCache(Protocol):
    async def get_messages(self, conversation_id: UUID) -> list[dict] | None: ...

    async def set_messages(self, conversation_id: UUID, messages: list[dict]) -> None: ...

    async def invalidate(self, conversation_id: UUID) -> None: ...


class ConversationRepository(Protocol):
    async def get(self, conversation_id: UUID) -> Conversation | None: ...

    def add(self, conversation: Conversation) -> None: ...


class MessageRepository(Protocol):
    def add(self, message: Message) -> None: ...

    async def list(self, conversation_id: UUID) -> list[Message]: ...

    async def recent(self, conversation_id: UUID, limit: int) -> list[Message]: ...


class AgentRunRepository(Protocol):
    def add(self, run: AgentRun) -> None: ...

    async def get(self, run_id: UUID) -> AgentRun | None: ...

    async def list(self, conversation_id: UUID) -> list[AgentRun]: ...

    async def active(self, conversation_id: UUID) -> AgentRun | None: ...


class UnitOfWork(Protocol):
    conversations: ConversationRepository
    messages: MessageRepository
    runs: AgentRunRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...

    async def flush(self) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
