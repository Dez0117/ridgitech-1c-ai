from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from backend.api.auth import current_user
from backend.dependencies import get_chat_service
from backend.models import User
from backend.schemas import (
    AgentRunRead,
    ChatAcceptedResponse,
    ConversationCreate,
    ConversationRead,
    ConversationSummary,
    MessageCreate,
    MessageRead,
)
from backend.services.chat import ChatService


router = APIRouter(prefix="/api/v1")
CurrentUser = Annotated[User, Depends(current_user)]


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(user: CurrentUser, service: ChatService = Depends(get_chat_service)):
    conversations = await service.list_conversations(user.id)
    return [
        ConversationSummary(**ConversationRead.model_validate(chat).model_dump(), preview=preview)
        for chat, preview in conversations
    ]


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
):
    return await service.create_conversation(user.id, payload.title)


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
):
    return await service.get_conversation(conversation_id, user.id)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
) -> None:
    await service.delete_conversation(conversation_id, user.id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: UUID,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
    fresh: bool = False,
):
    messages = await service.list_messages(conversation_id, user.id, fresh=fresh)
    return [MessageRead.model_validate(message) for message in messages]


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    response: Response,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
):
    conversation, user_message, run = await service.submit_message(conversation_id, user.id, payload.content)
    response.headers["Location"] = f"/api/v1/agent-runs/{run.id}"
    return ChatAcceptedResponse(
        conversation=ConversationRead.model_validate(conversation),
        user_message=MessageRead.model_validate(user_message),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunRead)
async def get_agent_run(
    run_id: UUID,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
):
    return await service.get_run(run_id, user.id)


@router.get("/conversations/{conversation_id}/agent-runs", response_model=list[AgentRunRead])
async def list_agent_runs(
    conversation_id: UUID,
    user: CurrentUser,
    service: ChatService = Depends(get_chat_service),
):
    return await service.list_runs(conversation_id, user.id)
