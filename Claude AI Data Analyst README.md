# PM Analyst

Natural-language data analyst for product managers. Ask a question, it asks back when the question is ambiguous, writes read-only SQL against your Postgres warehouse, and explains the result.

## Quick start (demo warehouse)

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY
docker compose up -d              # seeded demo Postgres on :5433 (read-only role: analyst_ro)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend
python -m app.cli crawl           # builds the schema docs (tables, columns, stats, FKs)
python -m app.cli ask "How's retention looking?"      # CLI test: should ask a question
uvicorn app.main:app --reload     # chat UI at http://localhost:8000
```

Pointing at a real warehouse: set `WAREHOUSE_DSN` (read-only role, ideally on a read replica) and `EXPOSED_SCHEMAS`, then re-crawl.

## How it works

```
Chat UI (SSE) -> FastAPI /api/chat -> agent loop (Claude + tools)
                                        |- search_schema / describe_table   (semantic layer)
                                        |- ask_user      (pauses the turn; answer resumes it)
                                        |- save_to_glossary (learned definitions)
                                        |- run_sql  -> guardrails -> EXPLAIN cost check -> read-only Postgres
                                        '- make_chart
```

- **Clarifying questions:** `ask_user` ends the HTTP stream with `awaiting_answer=true`. The user's next message is wrapped as that tool call's result and the loop continues. Policy lives in `backend/app/prompts.py`; capped at 2 rounds per question (`config.py`).
- **Learning:** answers that define a reusable term are saved to the glossary and injected into every future prompt, so the same question is never asked twice. Users can review/delete entries in the UI.
- **Semantic layer:** `semantic/metrics.yaml` (official metrics, never asked about) and `semantic/overrides.yaml` (table/column descriptions). Fill these in first for each customer.
- **Safety (3 layers):** sqlglot AST validation (single SELECT, table allowlist, blocked functions, no system catalogs) -> read-only role with timeouts -> EXPLAIN cost ceiling and a server-side cursor row cap. Every query is written to the `audit` table.

## Evals and tests

```bash
pytest tests/                 # guardrail unit tests (no DB or API key needed)
python evals/run_evals.py     # needs warehouse + API key; scores answer correctness and question precision
```
Add real PM questions to `evals/cases.yaml` as you collect them. `kind: clarify` cases must trigger a question; `kind: answer` cases must match the gold SQL result without asking.

## Known gaps (deliberate, for the MVP)

- Single tenant, no auth. Put it behind your SSO/reverse proxy before sharing.
- Schema retrieval is keyword-based (fine to a few hundred tables); swap `schema.search` for pgvector later.
- App state is SQLite; each table has `tenant_id` for a later move to Postgres.
- The UI does not reload past conversations; the backend already stores them.
- Prose numbers are not automatically verified against query output yet (next step: extract numbers and check them).
