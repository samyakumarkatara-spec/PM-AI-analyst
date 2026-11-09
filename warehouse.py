"""Read-only access to the customer's Postgres warehouse."""
import datetime as dt
import decimal
import time
import uuid

import psycopg

from .config import settings
from . import guardrails


class QueryError(Exception):
    """Query failed in a way the model can act on (message is model-safe)."""


def _conn_options() -> str:
    return (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={settings.statement_timeout_ms} "
        "-c idle_in_transaction_session_timeout=30000 "
        "-c lock_timeout=2000 "
        f"-c search_path={','.join(settings.exposed_schemas)}"
    )


def connect() -> psycopg.Connection:
    conn = psycopg.connect(settings.warehouse_dsn, options=_conn_options(), connect_timeout=10)
    conn.read_only = True
    return conn


def json_safe(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, memoryview)):
        return "<binary>"
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    return v


def explain_cost(conn: psycopg.Connection, sql: str) -> float:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql)
        plan = cur.fetchone()[0]
    return float(plan[0]["Plan"]["Total Cost"])


def run_query(sql: str, allowed_tables: set[str] | None) -> dict:
    """Validate, cost-check, then execute with a server-side cursor capped at max_rows."""
    cleaned = guardrails.validate(sql, allowed_tables, settings.exposed_schemas)
    t0 = time.time()
    try:
        with connect() as conn:
            cost = explain_cost(conn, cleaned)
            if cost > settings.max_plan_cost:
                raise QueryError(
                    f"Estimated query cost {cost:,.0f} exceeds the limit {settings.max_plan_cost:,.0f}. "
                    "Filter by a date range, aggregate earlier, or avoid joining large tables without filters."
                )
            with conn.cursor(name="analyst_cur") as cur:
                cur.execute(cleaned)
                rows = cur.fetchmany(settings.max_rows + 1)
                cols = [d.name for d in cur.description]
            conn.rollback()
    except psycopg.errors.QueryCanceled:
        raise QueryError(f"Query exceeded the {settings.statement_timeout_ms} ms timeout. Narrow it or aggregate earlier.")
    except psycopg.Error as e:
        raise QueryError(f"Postgres error: {str(e).strip()[:400]}")

    truncated = len(rows) > settings.max_rows
    rows = rows[: settings.max_rows]
    return {
        "sql": cleaned,
        "columns": cols,
        "rows": [[json_safe(v) for v in r] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "plan_cost": cost,
    }
