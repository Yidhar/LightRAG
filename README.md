# RagEngine

Graph-based RAG platform — knowledge bases per workspace, per-user isolation,
web UI for ingest / retrieval / graph exploration.

Built on top of the upstream LightRAG framework. This README covers **local
deployment only**. For core framework internals see [docs/](docs/).

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) 1.1+ (to build the WebUI)
- An LLM + embedding provider (OpenAI / Ollama / Azure / …)

---

## Quick start

```bash
# 1. Install Python deps
uv sync --extra api

# 2. Build the WebUI
cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..

# 3. One-shot bootstrap — generates .env, TOKEN_SECRET, and the auth DB
uv run python scripts/bootstrap_deployment.py

# 4. Configure your LLM provider
#    Open .env and fill in LLM_BINDING / LLM_MODEL / LLM_BINDING_API_KEY
#    and EMBEDDING_BINDING / EMBEDDING_MODEL / EMBEDDING_DIM.

# 5. Start the server
uv run lightrag-server
```

Open [http://localhost:9621](http://localhost:9621).

First time you open the login page it will show a **"首次使用：创建管理员账号"**
banner — pick a username and password (min 8 chars), submit, and you're in as
the first admin.

Additional users sign up themselves via the **注册** tab on the login page;
each registration auto-creates a private workspace owned by that user.

---

## What `bootstrap_deployment.py` does

Idempotent; safe to re-run.

1. Copies `env.example` → `.env` if missing.
2. Generates a 48-byte `TOKEN_SECRET` and writes it into `.env` (existing
   non-placeholder values are preserved).
3. Ensures the Platform V2 switches are set:
   - `USE_DB_AUTH=true`
   - `DB_URL=sqlite+aiosqlite:///./lightrag_auth.db`
   - `ENABLE_KB_ISOLATION=true`
   - `LIGHTRAG_ALLOW_SELF_REGISTRATION=true`
4. Creates the sqlite auth DB (users, workspaces, memberships, refresh
   tokens, audit log).

Pass `--skip-db` to defer schema creation to the server's next startup
(useful when the DB isn't reachable from the bootstrap host).

---

## Turning off self-registration

For private deployments where every account must be admin-invited, edit
`.env`:

```
LIGHTRAG_ALLOW_SELF_REGISTRATION=false
```

Restart. The 注册 tab disappears; administrators add users via the
**Members** page inside the WebUI.

---

## Production notes

- **`TOKEN_SECRET`** — rotate the auto-generated value before production.
  Keep `.env` out of git (already in `.gitignore`).
- **Database** — sqlite is the default for local dev. Point `DB_URL` at
  Postgres (`postgresql+asyncpg://user:pass@host/db`) for multi-worker
  deployments; no schema difference.
- **HTTPS** — set `SSL=true`, `SSL_CERTFILE`, `SSL_KEYFILE` in `.env`.
- **Workers** — `lightrag-gunicorn` runs gunicorn with multiple async
  workers.

---

## Docker

```bash
docker compose up -d
```

Compose files live at the repo root (`docker-compose.yml`,
`docker-compose-full.yml`). Generate a customised compose profile via
`scripts/setup/setup.sh`.

---

## Development

```bash
# Backend hot-reload
uv run uvicorn lightrag.api.lightrag_server:app --reload

# Frontend dev server (Vite on :5173, proxies /api to :9621)
cd lightrag_webui && bun run dev
```

Lint / tests:

```bash
ruff check .
uv run pytest tests                    # offline tests
uv run pytest tests --run-integration  # needs external services

cd lightrag_webui
bun run lint
bun test
```

---

## Reset / wipe local state

```bash
# Drop every platform table — identity, workspaces, memberships, audit log
uv run python scripts/reset_platform_db.py --confirm

# Re-run bootstrap to recreate the schema
uv run python scripts/bootstrap_deployment.py
```

Document storage lives under `./rag_storage/` by default — delete that
directory to wipe ingested documents / graphs / vectors.
