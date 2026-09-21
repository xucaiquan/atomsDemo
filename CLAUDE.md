# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Atoms Studio — a platform that turns a natural-language description into a working single-file HTML app. A three-stage LLM pipeline (requirement analysis → structure design → code generation) produces self-contained HTML, which is rendered in a locked-down sandbox iframe. Projects, versions, generation steps, and conversation history are persisted so users can iterate on prior versions.

App code lives under `app/` (not the repo root). The `app/backend/README.md` and `app/frontend/README.md` are the platform's own generated guides — read them before touching the backend or frontend respectively.

## Commands

All commands below assume the repo root unless noted.

```bash
# One-shot: create venv (uv), install both sides, allocate free ports,
# write env, start uvicorn + vite, wait for /health.
cd app && bash start_app_v2.sh
cd app && bash start_app_v2.sh --no-start   # install dependencies only

# Backend alone (needs DATABASE_URL + APP_AI_BASE_URL/APP_AI_KEY in env)
cd app/backend && uvicorn main:app --reload

# Frontend alone (proxies /api -> http://localhost:$BACKEND_PORT)
cd app/frontend && npm install && npm run dev

# Tests — MUST use `python -m pytest` from app/backend.
# There is no conftest.py/pyproject.toml, so the tests' `from services import ...`
# imports only resolve because `python -m` puts the CWD on sys.path.
cd app/backend && python -m pytest tests/ -q
cd app/backend && python -m pytest tests/test_context_memory.py::test_merge_continuation_removes_overlap -v

# Frontend checks
cd app/frontend && npm run lint     # eslint --quiet ./src
cd app/frontend && npm run build    # tsc + vite build
```

`start_app_v2.sh` picks the first free port pair starting at backend 8000 / frontend 3000 and retries on collision, so ports are not fixed. It also fetches env vars from a remote S2S API when `S2S_JWT_TOKEN`/`S2S_JWT_BASE_URL`/`CHAT_ID` are set, and otherwise reads `app/.env`.

## Architecture

### The 120-second constraint drives everything

The platform gateway has a 120s proxy read timeout, but the three-stage pipeline takes 1–4 minutes. So `POST /generate` is **asynchronous acceptance**, not a blocking call:

1. Router validates, persists a `pending` version + 3 pre-created `pending` steps + the user message, commits, returns `202 {"status":"accepted","version_seq":N}` in milliseconds (`GenerationPipeline.prepare`).
2. An `asyncio` background task runs the pipeline with its **own** DB session (`db_manager.session()`, not the request session, which is already closed).
3. The frontend polls `GET /versions/{seq}/steps` every 2.5s (`POLL_INTERVAL_MS` in `pages/Index.tsx`) until the version reaches `succeeded`/`failed`/`cancelled`.

Never reintroduce a synchronous wait inside the `generate` handler — that is exactly the bug this design exists to prevent.

### Database session boundary rule

Do not hold a SQLAlchemy transaction open across a slow external call (AI Hub, Stripe, HTTP). `services/pipeline.py` follows this pattern throughout: `_call_step` sets step status to `running` and **commits before** awaiting the model, then commits again when the step finishes. Each stage is a short DB phase bracketed by AI calls.

### Cancellation is state-first

`POST /versions/{seq}/cancel` persists `cancelled` to the version, its active steps, and the project, commits, and only *then* calls `task.cancel()` on the registered asyncio task (`_RUNNING_TASKS` in `routers/atoms.py`, keyed by `(public_id, seq)`).

Because a cancel can race with a model response already in flight, every write path in `pipeline.py` is guarded by `if version.status in ACTIVE_STATUSES` (`ACTIVE_STATUSES = ("pending", "running")`). A late-arriving success or failure must never overwrite `cancelled`. Preserve these guards when editing the pipeline — including the stage-boundary check at the top of `_call_step`, which refuses to start a new model call for a cancelled version.

### Status machine

`pending → running → succeeded | failed | cancelled` for versions and steps. `_recover_stale_versions` in `routers/atoms.py` sweeps versions stuck in `pending`/`running` for more than `STALE_AFTER` (10 min) to `failed` on list/detail/generate access — this handles a service restart. The frontend's `GENERATION_POLL_TIMEOUT_MS` (12 min) is deliberately longer than that threshold.

### Context memory and iteration

`generate` assembles `history_prompts` (all prior version prompts for the project, ascending) **before** `prepare()` writes the new version, then passes it to the background task along with `previous_html` (the newest `succeeded` version's HTML). The pipeline threads both into all three stages.

`services/prompts.py` owns the shaping: `build_history_block` caps history at `HISTORY_MAX_ITEMS = 6` items × `HISTORY_ITEM_CHARS = 200` chars each so context doesn't grow linearly across rounds, and `ANAPHORA_HINT` tells the model to resolve referential follow-ups like 「继续刚刚的需求」/「按之前说的」against that history rather than treating them as isolated new asks.

### HTML output reliability

LLM output is not reliably clean HTML, and a silent partial page is worse than an error. Three layers, in order:

- `services/html_extract.py` — `extract_html` tries markdown fence → first `<!DOCTYPE html>`/`<html` → raw fallback. Pure functions, no side effects, and the most-testable part of the repo.
- `is_complete_document` requires the result to end with `</html>`; truncated output is a failure, never a rendered page.
- `_generate_code` in `pipeline.py` recovers: continuation (resend the last `CONTINUE_TAIL_CHARS` and ask the model to resume, de-duplicating overlap via `_merge_continuation`), up to 2 continuation rounds, then a full rerun, then a user-readable `PipelineError`.

`inject_csp` adds the meta CSP tag if absent. Stage 3 uses `CODE_MODEL = "deepseek-v4-pro"` with `CODE_MAX_TOKENS = 16384` (stages 1–2 use `deepseek-v4-flash`) because reasoning tokens eat the output budget and content-heavy requests truncate easily.

### The sandbox constraint shapes the generated code

`PreviewPane.tsx` renders via `sandbox="allow-scripts allow-forms"` (a hardcoded constant, never a prop) — deliberately **without** `allow-same-origin`, so the iframe gets an opaque origin. Generated pages therefore cannot use `localStorage`, `sessionStorage`, `cookie`, `fetch`, `XMLHttpRequest`, `window.parent`, or any external CDN/font/image. The `CODE_SYSTEM` and `DESIGN_SYSTEM` prompts enforce this, and state must live in JS memory variables. If you change those prompts, keep these constraints — violating them produces a blank page.

### API contract (atoms)

`routers/atoms.py` is the API the frontend actually uses, separate from the auto-generated per-entity CRUD routers under `/api/v1/entities/*`.

- Only `public_id` (UUID v4) is exposed; the auto-increment `id` never appears in a response.
- Errors are always `{"error": {"code", "message"}}` with a user-facing Chinese `message` containing no stack trace or internal path. Codes: `VALIDATION_ERROR` 400, `NOT_FOUND` 404, `CONFLICT` 409, `UPSTREAM_ERROR` 502, `INTERNAL_ERROR` 500.
- `GET /versions/{seq}` is the **only** endpoint that returns `html`; list endpoints truncate step `output` to 2000 chars.
- One active generation per project: a second `generate` while a version is `pending`/`running` returns 409.

`frontend/src/lib/atoms.ts` is the single API layer — it wraps `client.apiCall.invoke` from `@metagptx/web-sdk` and both unwraps the error envelope into a thrown `Error` and handles backend 2xx responses that carry an error body.

## Constraints from the platform template

`app/backend/README.md` defines rules that override ordinary refactoring instincts:

- **Do not edit** `app/backend/core/**`, `app/backend/models/**`, `app/backend/main.py`, or `app/backend/lambda_handler.py`. These are platform-protected/auto-generated. `models/*.py` are generated from the JSON schemas in `data_models/` — change the schema, not the generated ORM.
- **Do not create user tables**; user management is handled by the platform's builtin table.
- Routers under `routers/` are **auto-discovered** by `include_routers_from_package` in `main.py` — never add a manual `include_router`. Every route prefix must start with `/api/v1/` to be proxied through Lambda.
- The generated ORM maintains `created_at`/`updated_at` — don't define or assign them.
- `core/config.py`'s `settings.__getattr__` reads *any* env var dynamically (`settings.app_ai_key` → `APP_AI_KEY`), always as a string. Relevant vars: `DATABASE_URL`, `APP_AI_BASE_URL`, `APP_AI_KEY`, `JWT_SECRET_KEY`, `OIDC_*`, `OSS_*`, `STRIPE_SECRET_KEY`.
- Read `app/backend/skills_docs/*.md` before implementing an AI capability, custom API, object storage, or payment feature. These are the platform's API references.
- `app/frontend/index.html` title/description/logo are managed by the overview system via `data-mgx-overview` markers — do not edit them.

## Working notes

- The repo's Chinese comments and docstrings are dense and intentional: they cite the spec IDs (`FR-###`, `US#`, `SC-###`, `research.md R#`) that motivated each decision, with `specs/001-atoms-demo/` under `app/uploads/` holding the source documents (`spec.md`, `plan.md`, `research.md`, `tasks.md`, `contracts/`). Read them before changing pipeline or contract behavior.
- `_RUNNING_TASKS` is process-local. Cancel only works within one worker process; multi-worker deployments would need a different mechanism.
- `app/backend/mock_data/` holds demo seed data; the pipeline does not depend on it.
