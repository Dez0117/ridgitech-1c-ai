import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.cache import ConversationCache, get_cache
from backend.config import settings
from backend.database import get_session
from backend.models import AgentRun, Conversation, Message
from backend.schemas import (
    AgentRunRead,
    ChatAcceptedResponse,
    ConversationCreate,
    ConversationRead,
    MessageCreate,
    MessageRead,
)
from backend.services.agent_runs import AgentRunProcessor, get_agent_run_processor


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


async def conversation_or_404(session: AsyncSession, conversation_id: UUID) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


async def recent_messages(session: AsyncSession, conversation_id: UUID, limit: int) -> list[Message]:
    if limit <= 0:
        return []
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(list(result)))


async def active_run_id(session: AsyncSession, conversation_id: UUID) -> UUID | None:
    return await session.scalar(
        select(AgentRun.id)
        .where(AgentRun.conversation_id == conversation_id, AgentRun.status == "running")
        .limit(1)
    )


def run_in_progress(run_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "RUN_IN_PROGRESS", "run_id": str(run_id)},
        headers={"Location": f"/api/v1/agent-runs/{run_id}"},
    )


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
    "/conversations/{conversation_id}/messages",
    response_model=ChatAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    session: AsyncSession = Depends(get_session),
    conversation_cache: ConversationCache = Depends(get_cache),
    processor: AgentRunProcessor = Depends(get_agent_run_processor),
):
    conversation = await conversation_or_404(session, conversation_id)
    current_run_id = await active_run_id(session, conversation_id)
    if current_run_id is not None:
        raise run_in_progress(current_run_id)
    previous_messages = await recent_messages(
        session,
        conversation_id,
        settings.agent_history_messages,
    )

    user_message = Message(conversation_id=conversation_id, role="user", content=payload.content)
    session.add(user_message)
    await session.flush()
    run = AgentRun(conversation_id=conversation_id, user_message_id=user_message.id, status="running")
    session.add(run)
    if conversation.title is None:
        conversation.title = payload.content.strip()[:255]
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        current_run_id = await active_run_id(session, conversation_id)
        if current_run_id is not None:
            raise run_in_progress(current_run_id)
        raise
    await session.refresh(user_message)
    await session.refresh(run)

    try:
        await conversation_cache.invalidate(conversation_id)
    except Exception:
        logger.warning("Redis invalidation failed", exc_info=True)

    background_tasks.add_task(
        processor.process,
        run.id,
        question=payload.content,
        organization_id=conversation.organization_id,
        history=history_text(previous_messages),
    )
    response.headers["Location"] = f"/api/v1/agent-runs/{run.id}"

    return ChatAcceptedResponse(
        conversation=ConversationRead.model_validate(conversation),
        user_message=MessageRead.model_validate(user_message),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunRead)
async def get_agent_run(run_id: UUID, session: AsyncSession = Depends(get_session)):
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    return run


@router.get("/conversations/{conversation_id}/agent-runs", response_model=list[AgentRunRead])
async def list_agent_runs(conversation_id: UUID, session: AsyncSession = Depends(get_session)):
    await conversation_or_404(session, conversation_id)
    result = await session.scalars(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.created_at, AgentRun.id)
    )
    return list(result)
