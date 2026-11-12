import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models import AgentResult

from backend.cache import ConversationCache, get_cache
from backend.config import settings
from backend.database import get_session
from backend.integrations.agent_service import AgentService, get_agent_service
from backend.models import AgentRun, Conversation, Message
from backend.schemas import (
    AgentRunRead,
    ChatResponse,
    ConversationCreate,
    ConversationRead,
    MessageCreate,
    MessageRead,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


async def conversation_or_404(session: AsyncSession, conversation_id: UUID) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


def history_text(messages: list[Message]) -> str:
    selected = messages[-settings.agent_history_messages :] if settings.agent_history_messages else []
    history = "\n".join(f"{message.role}: {message.content}" for message in selected)
    return history[-20_000:]


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(payload: ConversationCreate, session: AsyncSession = Depends(get_session)):
    conversation = Conversation(organization_id=payload.organization_id, title=payload.title)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def get_conversation(conversation_id: UUID, session: AsyncSession = Depends(get_session)):
    return await conversation_or_404(session, conversation_id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_session),
    conversation_cache: ConversationCache = Depends(get_cache),
):
    await conversation_or_404(session, conversation_id)
    try:
        cached = await conversation_cache.get_messages(conversation_id)
        if cached is not None:
            return [MessageRead.model_validate(item) for item in cached]
    except Exception:
        logger.warning("Redis read failed", exc_info=True)

    result = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
    )
    messages = list(result)
    serialized = [MessageRead.model_validate(message) for message in messages]
    try:
        await conversation_cache.set_messages(
            conversation_id, [message.model_dump(mode="json") for message in serialized]
        )
    except Exception:
        logger.warning("Redis write failed", exc_info=True)
    return serialized


@router.post(
    "/conversations/{conversation_id}/messages", response_model=ChatResponse, status_code=status.HTTP_201_CREATED
)
async def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    session: AsyncSession = Depends(get_session),
    conversation_cache: ConversationCache = Depends(get_cache),
    agent: AgentService = Depends(get_agent_service),
):
    conversation = await conversation_or_404(session, conversation_id)
    previous_result = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
    )
    previous_messages = list(previous_result)

    user_message = Message(conversation_id=conversation_id, role="user", content=payload.content)
    session.add(user_message)
    await session.flush()
    run = AgentRun(conversation_id=conversation_id, user_message_id=user_message.id, status="running")
    session.add(run)
    if conversation.title is None:
        conversation.title = payload.content.strip()[:255]
    await session.commit()
    await session.refresh(user_message)
    await session.refresh(run)

    try:
        result = await agent.run(
            payload.content,
            organization_id=conversation.organization_id,
            history=history_text(previous_messages),
        )
    except Exception:
        logger.exception("Unexpected agent integration error")
        result = AgentResult(
            status="failed",
            request_id=str(run.id),
            message="Сервис агента временно недоступен.",
            error={"code": "AGENT_UNAVAILABLE", "message": "Сервис агента временно недоступен."},
        )

    result_payload = result.model_dump(mode="json")
    assistant_message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=result.message,
        result=result_payload,
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
    await session.refresh(conversation)
    await session.refresh(assistant_message)
    await session.refresh(run)

    try:
        await conversation_cache.invalidate(conversation_id)
    except Exception:
        logger.warning("Redis invalidation failed", exc_info=True)

    return ChatResponse(
        conversation=ConversationRead.model_validate(conversation),
        user_message=MessageRead.model_validate(user_message),
        assistant_message=MessageRead.model_validate(assistant_message),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunRead)
async def get_agent_run(run_id: UUID, session: AsyncSession = Depends(get_session)):
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    return run
