# Backend API

Backend is an independent HTTP layer around the existing analytics agent. It does not modify the
agent graph or tools.

## Services

- `nginx`: reverse proxy for the UI and HTTP API on host port `8000`;
- `api`: FastAPI and the built React application on the internal Compose network;
- `worker`: ARQ process that executes the analytics agent;
- `postgres`: user accounts, JWT sessions, conversations, messages and agent runs;
- `redis`: durable worker queue and short-lived conversation-history cache.

Start the stack from the repository root:

```bash
docker compose up --build
```

The API runs `alembic upgrade head` before startup. The UI is available at
`http://localhost:8000/`. OpenAPI is available at
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

Register via `POST /api/v1/auth/register` with `name`, `email` and `password` (at least
12 characters), or log in via `POST /api/v1/auth/login` with `email` and `password`.
Both responses set an HttpOnly JWT cookie. Retain this cookie for subsequent API requests.
`GET /api/v1/auth/me` reads the signed-in profile. `DELETE /api/v1/auth/session`
revokes the current session in PostgreSQL and expires its cookie.

Chats and runs are scoped to the signed-in user. `GET /api/v1/conversations` lists their
chats with a preview of each chat's latest message. The demo agent uses `demo-org`
on the server. The migration preserves existing chats, their messages and their runs.
Existing chats become visible after an administrator assigns each verified owner in
`conversations.owner_id`.

`DELETE /api/v1/conversations/{conversation_id}` deletes the owner's chat, messages and agent runs.
It returns `409 Conflict` while a run is queued or running.

Set a stable `JWT_SECRET` in `.env` to keep sessions across API restarts. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. With an empty secret, a local
development process generates a temporary key. With `ENVIRONMENT=production`, a secret
of at least 32 bytes and HTTPS for the Secure cookie are required. `JWT_SESSION_MINUTES`
sets the session duration and defaults to 720 minutes.

Create a conversation:

```http
POST /api/v1/conversations
Content-Type: application/json

{}
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

For live frontend development, run `npm install` and `npm run dev` in `frontend/`
with a supported Node.js 22 release. Vite runs on `http://localhost:3000` and proxies
`/api` to `http://127.0.0.1:8000`. `npm run build` type-checks and builds the UI.
