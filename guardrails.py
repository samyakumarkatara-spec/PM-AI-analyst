"""SQL validation. Defense in depth: this is layer 1 of 3.

1. sqlglot AST checks (this file): one statement, SELECT-only, allowlisted tables,
   no dangerous functions.
2. Warehouse role: read-only, statement_timeout, read replica (see warehouse.py).
3. EXPLAIN cost ceiling + server-side cursor row cap (see warehouse.py).

Never rely on this file alone; the DB role is the real boundary.
"""
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class GuardrailError(ValueError):
    """Raised with a message that is safe and useful to show back to the model."""


def _types(*names):
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


FORBIDDEN_NODES = _types(
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge", "Command",
    "Into", "Copy", "Set", "Lock", "TruncateTable", "Grant", "Revoke", "Transaction", "Commit", "Rollback",
    "Use", "Pragma", "Analyze", "Kill", "LoadData",
)
QUERY_ROOTS = _types("Select", "Union", "Intersect", "Except", "SetOperation")

BLOCKED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "pg_ls_logdir", "pg_ls_waldir",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_open", "lo_create",
    "dblink", "dblink_exec", "dblink_connect", "set_config", "current_setting",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "pg_advisory_lock", "pg_advisory_xact_lock", "pg_try_advisory_lock",
    "pg_switch_wal", "pg_create_restore_point", "txid_current", "query_to_xml", "xpath",
    "pg_notify", "copy",
}


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    try:
        return node.sql_name().lower()
    except Exception:
        return type(node).__name__.lower()


def validate(sql: str, allowed_tables: set[str] | None, allowed_schemas: list[str]) -> str:
    """Return cleaned SQL or raise GuardrailError.

    allowed_tables: set of 'schema.table' names that were crawled/exposed (None = skip table allowlist).
    """
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        raise GuardrailError("Empty SQL.")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read="postgres") if s is not None]
    except ParseError as e:
        raise GuardrailError(f"SQL could not be parsed as PostgreSQL: {str(e)[:300]}")

    if len(statements) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, QUERY_ROOTS):
        raise GuardrailError("Only SELECT queries (optionally with CTEs) are allowed.")

    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node  # older sqlglot yields tuples
        if isinstance(node, FORBIDDEN_NODES):
            raise GuardrailError(f"Disallowed SQL construct: {type(node).__name__}.")
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name in BLOCKED_FUNCTIONS:
                raise GuardrailError(f"Function '{name}' is not allowed.")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    allowed_schemas_l = {s.lower() for s in allowed_schemas}

    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name:
            continue  # table-valued function etc.; function check above covers it
        schema = (t.db or "").lower()
        if t.args.get("catalog"):
            raise GuardrailError("Cross-database references are not allowed.")
        if not schema and name in cte_names:
            continue
        if schema in {"pg_catalog", "information_schema"} or name.startswith("pg_"):
            raise GuardrailError("System catalogs are not queryable. Use the schema tools instead.")
        if schema and schema not in allowed_schemas_l:
            raise GuardrailError(f"Schema '{schema}' is not exposed. Exposed schemas: {sorted(allowed_schemas_l)}.")
        if allowed_tables is not None:
            candidates = [f"{schema}.{name}"] if schema else [f"{s}.{name}" for s in allowed_schemas_l]
            if not any(c in allowed_tables for c in candidates):
                raise GuardrailError(
                    f"Table '{t.name}' is not in the exposed schema. Use search_schema to find valid tables."
                )

    return cleaned
