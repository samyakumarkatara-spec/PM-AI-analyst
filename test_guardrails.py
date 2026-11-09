import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.guardrails import GuardrailError, validate  # noqa: E402

ALLOWED = {"public.users", "public.orders"}
SCHEMAS = ["public"]


def ok(sql):
    return validate(sql, ALLOWED, SCHEMAS)


def bad(sql):
    with pytest.raises(GuardrailError):
        validate(sql, ALLOWED, SCHEMAS)


def test_simple_select():
    assert ok("SELECT count(*) FROM users;").startswith("SELECT")


def test_cte_and_join():
    ok("WITH u AS (SELECT * FROM public.users) SELECT count(*) FROM u JOIN orders o ON o.user_id = u.id")


def test_union():
    ok("SELECT id FROM users UNION ALL SELECT id FROM orders")


@pytest.mark.parametrize("sql", [
    "DROP TABLE users",
    "DELETE FROM users",
    "UPDATE users SET plan='x'",
    "INSERT INTO users(email) VALUES ('a')",
    "SELECT 1; DROP TABLE users",
    "SELECT * INTO newt FROM users",
    "SELECT * FROM users FOR UPDATE",
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM secret.payroll",
    "SELECT * FROM not_exposed",
    "COPY users TO '/tmp/x'",
    "SET statement_timeout = 0",
    "",
])
def test_blocked(sql):
    bad(sql)
