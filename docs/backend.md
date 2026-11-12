# Backend API

Backend is an independent HTTP layer around the existing analytics agent. It does not modify the
agent graph or tools.

## Services

- `api`: FastAPI application on port `8000`;
- `worker`: ARQ process that executes the analytics agent;
- `postgres`: conversation, message and agent-run history;
- `redis`: durable worker queue and short-lived conversation-history cache.

Start the stack from the repository root:

```bash
docker compose up --build
```

The API runs `alembic upgrade head` before startup. OpenAPI is available at
`http://localhost:8000/docs` and health status at `http://localhost:8000/health`.

Set `OPENROUTER_API_KEY` in a local `.env` file to execute the real model. The current agent uses
its existing `FixtureBackend` for synthetic 1C data; a real 1C HTTP adapter can replace it inside
`backend/integrations/agent_service.py` when that API contract is available.

## Layers

- `api/routes.py` maps HTTP requests and responses only;
- `services/chat.py` implements conversation and queue use cases;
- `services/agent_runs.py` implements idempotent worker execution;
- `protocols.py` defines repositories, queue, cache, agent, and unit-of-work ports;
- `repositories/` contains SQLAlchemy implementations;
- `queue.py` contains the ARQ adapter;
- `worker.py` is the separate worker entrypoint.

The API process does not import or invoke the analytics agent. It commits a `queued`
run and sends only its immutable input to Redis. The worker changes the status to
`running`, invokes the unchanged agent, and persists the terminal result.

## Chat flow

Create a conversation:

```http
POST /api/v1/conversations
Content-Type: application/json

{"organization_id":"demo-org"}
```

Send a message and enqueue an agent run:

```http
POST /api/v1/conversations/{conversation_id}/messages
Content-Type: application/json

{"content":"Посчитай продажи за январь 2025"}
```

The endpoint responds with `202 Accepted`, a run with the `queued` status, and a
`Location` header pointing to `/api/v1/agent-runs/{run_id}`. Poll that URL until the
status becomes `completed`, `needs_clarification`, or `failed`, then read the new
assistant message from the conversation history.

Only one run may be active per conversation. A second message submitted while a run
is `queued` or `running` receives `409 Conflict` with the active run ID and its
`Location`. Jobs use the run UUID as the ARQ job ID, and the worker safely ignores a
job whose run is already terminal.

Read history:

```http
GET /api/v1/conversations/{conversation_id}/messages
```

Inspect a persisted run:

```http
GET /api/v1/agent-runs/{run_id}
```

List the run journal for a conversation:

```http
GET /api/v1/conversations/{conversation_id}/agent-runs
```

## Development

```bash
python -m pip install -e ".[backend,test]"
pytest
```

Run the API and worker without Docker:

```bash
alembic upgrade head
uvicorn backend.main:app --reload
arq backend.worker.WorkerSettings
```
