import asyncio
import os
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agent.models import AgentResult
from backend.cache import get_cache
from backend.database import Base, get_session
from backend.integrations.agent_service import get_agent_service
from backend.main import app


class FakeCache:
    def __init__(self):
        self.values = {}

    async def get_messages(self, conversation_id):
        return self.values.get(str(conversation_id))

    async def set_messages(self, conversation_id, messages):
        self.values[str(conversation_id)] = messages

    async def invalidate(self, conversation_id):
        self.values.pop(str(conversation_id), None)

    async def ping(self):
        return True


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def run(self, question, *, organization_id, history):
        self.calls.append({"question": question, "organization_id": organization_id, "history": history})
        return AgentResult(
            status="completed",
            request_id=uuid4().hex,
            message=f"Ответ на: {question}",
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            model_calls=1,
            elapsed_seconds=0.01,
        )


class FailingAgent:
    async def run(self, question, *, organization_id, history):
        raise RuntimeError("agent is unavailable")


def test_chat_flow_persists_history():
    test_engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    fake_cache = FakeCache()
    fake_agent = FakeAgent()

    async def session_override():
        async with session_factory() as session:
            yield session

    async def prepare():
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_cache] = lambda: fake_cache
    app.dependency_overrides[get_agent_service] = lambda: fake_agent
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/conversations", json={"organization_id": "  demo-org  "})
            assert created.status_code == 201
            assert created.json()["organization_id"] == "demo-org"
            conversation_id = created.json()["id"]

            response = client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json={"content": "Продажи за январь"}
            )
            assert response.status_code == 201
            assert response.json()["run"]["status"] == "completed"
            assert response.json()["assistant_message"]["content"] == "Ответ на: Продажи за январь"

            history = client.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert history.status_code == 200
            assert [message["role"] for message in history.json()] == ["user", "assistant"]

            runs = client.get(f"/api/v1/conversations/{conversation_id}/agent-runs")
            assert runs.status_code == 200
            assert len(runs.json()) == 1
            assert runs.json()[0]["id"] == response.json()["run"]["id"]

            blank = client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json={"content": "   "}
            )
            assert blank.status_code == 422

            trimmed = client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json={"content": "  Поступления  "}
            )
            assert trimmed.status_code == 201
            assert trimmed.json()["user_message"]["content"] == "Поступления"
            assert fake_agent.calls[-1]["history"] == "user: Продажи за январь\nassistant: Ответ на: Продажи за январь"

            app.dependency_overrides[get_agent_service] = lambda: FailingAgent()
            failed = client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json={"content": "Задолженность"}
            )
            assert failed.status_code == 201
            assert failed.json()["run"]["status"] == "failed"
            assert failed.json()["run"]["error"]["code"] == "AGENT_UNAVAILABLE"

            runs = client.get(f"/api/v1/conversations/{conversation_id}/agent-runs")
            assert [run["status"] for run in runs.json()] == ["completed", "completed", "failed"]
    finally:
        app.dependency_overrides.clear()
        asyncio.run(test_engine.dispose())
