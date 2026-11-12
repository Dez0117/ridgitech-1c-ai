from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    organization_id: str = Field(default="demo-org", min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=255)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=10_000)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    result: dict[str, Any] | None
    created_at: datetime


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None
    request_id: str | None
    status: str
    error: dict[str, Any] | None
    usage: dict[str, int]
    cost_usd: str | None
    model_calls: int
    elapsed_seconds: float
    created_at: datetime
    completed_at: datetime | None


class ChatAcceptedResponse(BaseModel):
    conversation: ConversationRead
    user_message: MessageRead
    run: AgentRunRead


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
