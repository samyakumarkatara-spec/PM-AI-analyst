"""Schema crawler + retrieval.

Crawl = information_schema/pg_catalog metadata + cheap pg_stats profiling + your
hand-written overrides (semantic/overrides.yaml). Retrieval is lexical for the MVP
(fine up to a few hundred tables); swap `search()` for pgvector when you outgrow it.
"""
import re
from pathlib import Path

import yaml

from . import store, warehouse
from .config import settings

SEMANTIC_DIR = Path(__file__).resolve().parents[2] / "semantic"

COLUMNS_SQL = """
SELECT n.nspname, c.relname, c.relkind, a.attname, format_type(a.atttypid, a.atttypmod),
       col_description(c.oid, a.attnum), obj_description(c.oid, 'pg_class'), c.reltuples::bigint
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','v','m','p')
ORDER BY n.nspname, c.relname, a.attnum
"""

STATS_SQL = """
SELECT schemaname, tablename, attname, n_distinct, null_frac, most_common_vals::text
FROM pg_stats WHERE schemaname = ANY(%s)
"""

FK_SQL = """
SELECT sn.nspname, s.relname, pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class s ON s.oid = c.conrelid
JOIN pg_namespace sn ON sn.oid = s.relnamespace
WHERE c.contype = 'f' AND sn.nspname = ANY(%s)
"""


def _load_yaml(name: str) -> dict:
    p = SEMANTIC_DIR / name
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def load_metrics() -> list[dict]:
    return _load_yaml("metrics.yaml").get("metrics", [])


def _parse_mcv(text: str | None) -> list[str]:
    if not text:
        return []
    items = [x.strip().strip('"') for x in text.strip("{}").split(",")]
    return [x for x in items if x][:8]


def crawl() -> dict[str, dict]:
    schemas = settings.exposed_schemas
    overrides = _load_yaml("overrides.yaml").get("tables", {})
    docs: dict[str, dict] = {}

    with warehouse.connect() as conn, conn.cursor() as cur:
        cur.execute(COLUMNS_SQL, (schemas,))
        for schema, table, kind, col, typ, col_comment, tbl_comment, reltuples in cur.fetchall():
            key = f"{schema}.{table}"
            d = docs.setdefault(key, {
                "table": key,
                "kind": {"r": "table", "p": "table", "v": "view", "m": "materialized view"}[kind],
                "description": tbl_comment or "",
                "row_estimate": int(reltuples) if reltuples is not None and reltuples >= 0 else None,
                "columns": [], "foreign_keys": [],
            })
            d["columns"].append({"name": col, "type": typ, "description": col_comment or ""})

        cur.execute(STATS_SQL, (schemas,))
        stats = {(s, t, a): (nd, nf, mcv) for s, t, a, nd, nf, mcv in cur.fetchall()}
        for key, d in docs.items():
            schema, table = key.split(".", 1)
            for col in d["columns"]:
                s = stats.get((schema, table, col["name"]))
                if not s:
                    continue
                nd, nf, mcv = s
                rows = d["row_estimate"] or 0
                distinct = nd if nd and nd > 0 else (-nd * rows if nd else None)
                col["null_frac"] = round(float(nf), 3) if nf is not None else None
                col["approx_distinct"] = int(distinct) if distinct else None
                if distinct and distinct <= 30:
                    col["common_values"] = _parse_mcv(mcv)

        cur.execute(FK_SQL, (schemas,))
        for schema, table, definition in cur.fetchall():
            key = f"{schema}.{table}"
            if key in docs:
                docs[key]["foreign_keys"].append(definition)

        conn.rollback()

    # Hand-written descriptions win over auto-generated ones.
    for key, ov in overrides.items():
        key = key if "." in key else f"{schemas[0]}.{key}"
        if key not in docs:
            continue
        if ov.get("description"):
            docs[key]["description"] = ov["description"]
        for cname, cdesc in (ov.get("columns") or {}).items():
            for col in docs[key]["columns"]:
                if col["name"] == cname:
                    col["description"] = cdesc

    store.save_schema_docs(docs)
    return docs


# ---------- retrieval & formatting ----------
def _tokens(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text.lower())}


def _score(doc: dict, q: set[str]) -> float:
    score = 0.0
    score += 3 * len(q & _tokens(doc["table"].replace("_", " ")))
    score += 1.5 * len(q & _tokens(doc["description"]))
    for c in doc["columns"]:
        score += 2 * len(q & _tokens(c["name"].replace("_", " ")))
        score += 1 * len(q & _tokens(c.get("description", "")))
        score += 1 * len(q & _tokens(" ".join(c.get("common_values", []))))
    return score


def summarize(doc: dict) -> str:
    cols = ", ".join(f"{c['name']} {c['type']}" for c in doc["columns"])
    rows = f"~{doc['row_estimate']:,} rows" if doc.get("row_estimate") else "row count unknown"
    desc = f" - {doc['description']}" if doc["description"] else ""
    return f"{doc['table']} ({doc['kind']}, {rows}){desc}\n  columns: {cols}"


def describe(doc: dict) -> str:
    lines = [summarize(doc).split("\n")[0]]
    for c in doc["columns"]:
        extra = []
        if c.get("description"):
            extra.append(c["description"])
        if c.get("common_values"):
            extra.append("values: " + ", ".join(c["common_values"]))
        if c.get("approx_distinct"):
            extra.append(f"~{c['approx_distinct']} distinct")
        if c.get("null_frac"):
            extra.append(f"{int(c['null_frac'] * 100)}% null")
        lines.append(f"  - {c['name']} {c['type']}" + (f"  [{'; '.join(extra)}]" if extra else ""))
    for fk in doc.get("foreign_keys", []):
        lines.append(f"  FK: {fk}")
    return "\n".join(lines)


def search(docs: dict[str, dict], query: str, k: int = 8) -> list[dict]:
    q = _tokens(query)
    if not q:
        return list(docs.values())[:k]
    ranked = sorted(docs.values(), key=lambda d: _score(d, q), reverse=True)
    return [d for d in ranked if _score(d, q) > 0][:k]


def resolve_table(docs: dict[str, dict], name: str) -> dict | None:
    name = name.strip().lower().replace('"', "")
    if name in docs:
        return docs[name]
    for key, d in docs.items():
        if key.split(".", 1)[1] == name:
            return d
    return None
