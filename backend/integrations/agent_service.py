from datetime import datetime
import asyncio
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.fixture_backend import FixtureBackend
from agent.graph import AnalyticsAgent
from agent.models import AgentResult, AgentSettings
from agent.providers import create_qwen_model
from tools import BackendError

from backend.config import settings
from .onec_data_backend import OneCDataBackend


class AgentService:
    """Подключает агент к выбранному источнику аналитических данных."""

    async def run(self, question: str, *, organization_id: str, history: str) -> AgentResult:
        request_id = uuid4().hex
        analytics_backend = (OneCDataBackend(settings.onec_data_url, settings.onec_data_token)
                             if settings.analytics_backend == "onec" else FixtureBackend())
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.agent_timeout_seconds):
                if isinstance(analytics_backend, OneCDataBackend):
                    context = await analytics_backend.context(
                        request_id, timeout_seconds=settings.agent_timeout_seconds,
                        allow_demo_metrics=settings.onec_allow_demo_metrics,
                    )
                else:
                    context = analytics_backend.context(request_id, timeout_seconds=settings.agent_timeout_seconds)
                remaining = settings.agent_timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                from dataclasses import replace

                context = replace(context, timeout_seconds=remaining)
                if organization_id not in context.allowed_organization_ids:
                    raise BackendError("ACCESS_DENIED", "Укажите доступную организацию из каталога 1С.")
                model = create_qwen_model()
                agent = AnalyticsAgent(model, analytics_backend, settings=AgentSettings())
                return await agent.arun(
                    question, context,
                    current_date=datetime.now(ZoneInfo("Europe/Moscow")).date(),
                    timezone="Europe/Moscow", selected_organization_id=organization_id, history=history,
                )
        except TimeoutError:
            message = "Истекло общее время выполнения запроса."
            return AgentResult(
                status="failed", request_id=request_id, message=message,
                error={"code": "TIMEOUT", "message": message, "retryable": True},
                elapsed_seconds=time.monotonic() - started,
            )
        except BackendError as exc:
            return AgentResult(
                status="failed", request_id=request_id, message=exc.message,
                error={"code": exc.code, "message": exc.message, "retryable": exc.retryable,
                       **({"details": exc.details} if exc.details else {})},
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            if isinstance(analytics_backend, OneCDataBackend):
                await analytics_backend.close()


agent_service = AgentService()


def get_agent_service() -> AgentService:
    return agent_service
