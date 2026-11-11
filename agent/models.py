from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_turns: int = Field(default=8, ge=1, le=20)
    max_parallel_steps: int = Field(default=2, ge=1, le=8)


class AgentResult(BaseModel):
    status: Literal["completed", "needs_clarification", "failed"]
    request_id: str
    message: str
    plan: dict | None = None
    results: list[dict] = Field(default_factory=list)
    comparisons: list[dict] = Field(default_factory=list)
    sources: list[Any] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: str | None = None
    model_calls: int = 0
    elapsed_seconds: float = 0
    prompt_hash: str = ""
    tools_hash: str = ""
    error: dict | None = None


class TerminalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["needs_clarification", "unsupported"]
    message: str = Field(min_length=1, max_length=2000)
