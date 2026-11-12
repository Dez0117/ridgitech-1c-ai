import asyncio
import os
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agent.models import AgentResult
from backend.database import Base
from backend.dependencies import get_chat_service
from backend.main import create_app
from backend.repositories import SqlAlchemyUnitOfWork
from backend.services.agent_runs import AgentRunService
from backend.services.chat import ChatService


class FakeCache:
    def __init__(self):
        self.values = {}

    async def get_messages(self, conversation_id):
        return self.values.get(str(conversation_id))

    async def set_messages(self, conversation_id, messages):
        self.values[str(conversation_id)] = messages

    async def invalidate(self, conversation_id):
        self.values.pop(str(conversation_id), None)


class FakeQueue:
    def __init__(self):
        self.jobs = []
        self.fail = False

    async def enqueue(self, run_id, *, question, organization_id, history):
        if self.fail:
            raise ConnectionError("queue unavailable")
        self.jobs.append(
            {
                "run_id": run_id,
                "question": question,
                "organization_id": organization_id,
                "history": history,
            }
        )
        return str(run_id)

    async def worker_alive(self):
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


def test_api_enqueues_and_worker_persists_result():
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    fake_cache = FakeCache()
    fake_queue = FakeQueue()
    fake_agent = FakeAgent()

    def create_uow():
        return SqlAlchemyUnitOfWork(session_factory)

    chat_service = ChatService(create_uow, fake_queue, fake_cache, history_messages=20)
    worker_service = AgentRunService(create_uow, fake_cache, fake_agent)
    app = create_app(manage_resources=False)
    app.dependency_overrides[get_chat_service] = lambda: chat_service

    async def prepare():
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def execute_last_job(service=worker_service):
        job = fake_queue.jobs[-1]
        await service.execute(
            job["run_id"],
            question=job["question"],
            organization_id=job["organization_id"],
            history=job["history"],
        )

    asyncio.run(prepare())
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/conversations", json={"organization_id": "  demo-org  "})
            assert created.status_code == 201
            conversation_id = created.json()["id"]

            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "Продажи за январь"},
            )
            assert accepted.status_code == 202
            assert accepted.json()["run"]["status"] == "queued"
            assert accepted.headers["location"] == f"/api/v1/agent-runs/{accepted.json()['run']['id']}"
            assert len(fake_queue.jobs) == 1

            history = client.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert [message["role"] for message in history.json()] == ["user"]

            asyncio.run(execute_last_job())
            run = client.get(accepted.headers["location"])
            assert run.json()["status"] == "completed"
            history = client.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert [message["role"] for message in history.json()] == ["user", "assistant"]

            asyncio.run(execute_last_job())
            history = client.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert [message["role"] for message in history.json()] == ["user", "assistant"]

            second = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "  Поступления  "},
            )
            assert second.status_code == 202
            assert second.json()["user_message"]["content"] == "Поступления"
            assert fake_queue.jobs[-1]["history"] == (
                "user: Продажи за январь\nassistant: Ответ на: Продажи за январь"
            )
            asyncio.run(execute_last_job())

            third = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "Задолженность"},
            )
            assert third.status_code == 202
            failing_worker = AgentRunService(create_uow, fake_cache, FailingAgent())
            asyncio.run(execute_last_job(failing_worker))
            failed = client.get(third.headers["location"])
            assert failed.json()["status"] == "failed"
            assert failed.json()["error"]["code"] == "AGENT_UNAVAILABLE"

            active = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "Активный запрос"},
            )
            conflict = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "Ещё один запрос"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["run_id"] == active.json()["run"]["id"]

            asyncio.run(execute_last_job())
            new_conversation = client.post("/api/v1/conversations", json={"organization_id": "demo-org"})
            fake_queue.fail = True
            unavailable = client.post(
                f"/api/v1/conversations/{new_conversation.json()['id']}/messages",
                json={"content": "Продажи"},
            )
            assert unavailable.status_code == 503
            failed_run = client.get(unavailable.headers["location"])
            assert failed_run.json()["status"] == "failed"
            assert failed_run.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    finally:
        app.dependency_overrides.clear()
        asyncio.run(test_engine.dispose())
