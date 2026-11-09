"""Tool definitions + executor. ask_user is NOT executed here: the agent loop
treats it as a control-flow signal (pause, render question, resume on answer)."""
import json

from . import schema, store, warehouse
from .guardrails import GuardrailError

TOOLS = [
    {
        "name": "search_schema",
        "description": "Find tables relevant to a topic by keyword (table names, column names, descriptions, common values). Returns compact summaries. Use an empty query to list everything.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "describe_table",
        "description": "Full detail for one table: every column with type, description, null rate, approximate distinct count, common values for categorical columns, and foreign keys. Call this before writing SQL so you use real column values.",
        "input_schema": {"type": "object", "properties": {"table": {"type": "string", "description": "schema.table or bare table name"}}, "required": ["table"]},
    },
    {
        "name": "run_sql",
        "description": "Run one read-only PostgreSQL SELECT query against the warehouse. Returns columns, a preview of rows, and per-column stats. Results above the row cap are truncated.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "purpose": {"type": "string", "description": "Short plain-English description of what this query measures, shown to the user."},
            },
            "required": ["sql", "purpose"],
        },
    },
    {
        "name": "make_chart",
        "description": "Render a chart in the chat from a previous run_sql result. Use only when a visual clearly helps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "result_id": {"type": "integer"},
                "chart_type": {"type": "string", "enum": ["bar", "line", "scatter"]},
                "x": {"type": "string", "description": "Column for the x axis / categories"},
                "y": {"type": "array", "items": {"type": "string"}, "description": "One or more numeric columns"},
                "title": {"type": "string"},
            },
            "required": ["result_id", "chart_type", "x", "y", "title"],
        },
    },
    {
        "name": "ask_user",
        "description": "Ask the user ONE clarifying question when the answer would materially change the SQL or the number. Pauses the conversation until the user answers. Do not call together with other tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4,
                            "description": "Concrete options grounded in the real schema/data"},
                "why_it_matters": {"type": "string", "description": "One sentence: how the choice changes the result"},
            },
            "required": ["question", "options", "why_it_matters"],
        },
    },
    {
        "name": "save_to_glossary",
        "description": "Remember a reusable business definition the user just clarified (e.g. 'active user'), so you don't ask again. Not for one-off choices like date ranges.",
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}, "definition": {"type": "string", "description": "Precise definition, including source table/columns and thresholds"}},
            "required": ["term", "definition"],
        },
    },
]


def _col_stats(columns, rows):
    stats = {}
    for i, name in enumerate(columns):
        vals = [r[i] for r in rows]
        non_null = [v for v in vals if v is not None]
        s = {"nulls": len(vals) - len(non_null)}
        nums = [v for v in non_null if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if non_null and len(nums) == len(non_null):
            s.update(min=min(nums), max=max(nums), mean=round(sum(nums) / len(nums), 4), sum=round(sum(nums), 4))
        else:
            s["distinct"] = len({json.dumps(v, default=str) for v in non_null})
        stats[name] = s
    return stats


class ToolRunner:
    """One instance per agent turn. Collects UI events in self.events."""

    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self.docs = store.load_schema_docs()
        self.allowed = set(self.docs) or None
        self.results: dict[int, dict] = {}
        self.events: list[dict] = []

    def drain(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    # returns (content_str, is_error)
    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None:
                return f"Unknown tool: {name}", True
            return fn(**args), False
        except (GuardrailError, warehouse.QueryError) as e:
            return str(e), True
        except TypeError as e:
            return f"Bad arguments for {name}: {e}", True
        except Exception as e:  # never crash the loop on a tool bug
            return f"Tool {name} failed: {type(e).__name__}: {e}", True

    def _t_search_schema(self, query: str) -> str:
        if not self.docs:
            return "No schema has been crawled yet. Run `python -m app.cli crawl`."
        hits = schema.search(self.docs, query) if query.strip() else list(self.docs.values())
        if not hits:
            return "No matching tables. Try broader keywords or an empty query to list all tables."
        return "\n\n".join(schema.summarize(d) for d in hits)

    def _t_describe_table(self, table: str) -> str:
        doc = schema.resolve_table(self.docs, table)
        if not doc:
            return f"Table '{table}' not found. Use search_schema."
        return schema.describe(doc)

    def _t_run_sql(self, sql: str, purpose: str = "") -> str:
        self.events.append({"type": "tool", "label": purpose or "Running query"})
        try:
            res = warehouse.run_query(sql, self.allowed)
        except (GuardrailError, warehouse.QueryError) as e:
            store.audit(self.conversation_id, purpose, sql, "error", error=str(e))
            self.events.append({"type": "sql", "sql": sql, "purpose": purpose, "error": str(e)})
            raise
        store.audit(self.conversation_id, purpose, res["sql"], "ok", res["row_count"], res["elapsed_ms"])
        rid = len(self.results) + 1
        self.results[rid] = res
        self.events.append({"type": "sql", "sql": res["sql"], "purpose": purpose})
        self.events.append({
            "type": "table", "result_id": rid, "columns": res["columns"], "rows": res["rows"][:200],
            "row_count": res["row_count"], "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"],
        })
        out = {
            "result_id": rid, "columns": res["columns"], "row_count": res["row_count"],
            "truncated": res["truncated"], "preview_rows": res["rows"][:30],
            "column_stats": _col_stats(res["columns"], res["rows"]),
        }
        if res["truncated"]:
            out["warning"] = "Result hit the row cap and is PARTIAL. Aggregate in SQL before drawing conclusions."
        if res["row_count"] == 0:
            out["warning"] = "Query returned zero rows. Check filters and actual column values before concluding."
        return json.dumps(out, default=str)

    def _t_make_chart(self, result_id: int, chart_type: str, x: str, y: list, title: str) -> str:
        res = self.results.get(result_id)
        if not res:
            return f"No result with id {result_id} in this turn. Re-run the query first."
        cols = res["columns"]
        for c in [x, *y]:
            if c not in cols:
                return f"Column '{c}' not in result. Available: {cols}"
        xi, yis = cols.index(x), [cols.index(c) for c in y]
        rows = res["rows"][:200]
        if chart_type == "scatter":
            series = [{"name": c, "points": [{"x": r[xi], "y": r[yi]} for r in rows]} for c, yi in zip(y, yis)]
            spec = {"labels": [], "series": series}
        else:
            spec = {"labels": [str(r[xi]) for r in rows],
                    "series": [{"name": c, "values": [r[yi] for r in rows]} for c, yi in zip(y, yis)]}
        self.events.append({"type": "chart", "chart_type": chart_type, "title": title, "x_label": x, **spec})
        return "Chart rendered for the user."

    def _t_save_to_glossary(self, term: str, definition: str) -> str:
        store.glossary_upsert(term, definition)
        self.events.append({"type": "glossary", "term": term.strip().lower(), "definition": definition})
        return f"Saved '{term}' to the glossary."
