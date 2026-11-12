# Backend API

Backend is an independent HTTP layer around the existing analytics agent. It does not modify the
agent graph or tools.

## Services

- `api`: FastAPI application on port `8000`;
- `postgres`: conversation, message and agent-run history;
- `redis`: short-lived conversation-history cache.

Start the stack from the repository root:

```bash
docker compose up --build
```

The API runs `alembic upgrade head` before startup. OpenAPI is available at
`http://localhost:8000/docs` and health status at `http://localhost:8000/health`.

Set `OPENROUTER_API_KEY` in a local `.env` file to execute the real model. The current agent uses
its existing `FixtureBackend` for synthetic 1C data; a real 1C HTTP adapter can replace it inside
`backend/integrations/agent_service.py` when that API contract is available.

## Chat flow

Create a conversation:

```http
POST /api/v1/conversations
Content-Type: application/json

{"organization_id":"demo-org"}
```

Send a message and synchronously invoke the agent:

```http
POST /api/v1/conversations/{conversation_id}/messages
Content-Type: application/json

{"content":"Посчитай продажи за январь 2025"}
```

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
