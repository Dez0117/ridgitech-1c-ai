from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.fixture_backend import FixtureBackend
from agent.graph import AnalyticsAgent
from agent.models import AgentResult, AgentSettings
from agent.providers import create_qwen_model

from backend.config import settings


class AgentService:
    """Adapts the existing agent to the backend without changing agent code."""

    async def run(self, question: str, *, organization_id: str, history: str) -> AgentResult:
        request_id = uuid4().hex
        analytics_backend = FixtureBackend()
        model = create_qwen_model()
        agent = AnalyticsAgent(model, analytics_backend, settings=AgentSettings())
        context = analytics_backend.context(request_id, timeout_seconds=settings.agent_timeout_seconds)
        return await agent.arun(
            question,
            context,
            current_date=datetime.now(ZoneInfo("Europe/Moscow")).date(),
            timezone="Europe/Moscow",
            selected_organization_id=organization_id,
            history=history,
        )


agent_service = AgentService()


def get_agent_service() -> AgentService:
    return agent_service
