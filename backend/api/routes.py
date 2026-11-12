from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from backend.dependencies import get_chat_service
from backend.schemas import (
    AgentRunRead,
    ChatAcceptedResponse,
    ConversationCreate,
    ConversationRead,
    MessageCreate,
    MessageRead,
)
from backend.services.chat import ChatService


router = APIRouter(prefix="/api/v1")


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    service: ChatService = Depends(get_chat_service),
):
    return await service.create_conversation(payload.organization_id, payload.title)


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: UUID,
    service: ChatService = Depends(get_chat_service),
):
    return await service.get_conversation(conversation_id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: UUID,
    service: ChatService = Depends(get_chat_service),
):
    messages = await service.list_messages(conversation_id)
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
    service: ChatService = Depends(get_chat_service),
):
    conversation, user_message, run = await service.submit_message(conversation_id, payload.content)
    response.headers["Location"] = f"/api/v1/agent-runs/{run.id}"
    return ChatAcceptedResponse(
        conversation=ConversationRead.model_validate(conversation),
        user_message=MessageRead.model_validate(user_message),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunRead)
async def get_agent_run(
    run_id: UUID,
    service: ChatService = Depends(get_chat_service),
):
    return await service.get_run(run_id)


@router.get("/conversations/{conversation_id}/agent-runs", response_model=list[AgentRunRead])
async def list_agent_runs(
    conversation_id: UUID,
    service: ChatService = Depends(get_chat_service),
):
    return await service.list_runs(conversation_id)
