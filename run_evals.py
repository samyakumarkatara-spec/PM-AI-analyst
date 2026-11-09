"""Eval harness. Run from the project root:  python evals/run_evals.py

Scores two things:
  - correctness: for `answer` cases, does the agent's final result match the gold SQL's result?
  - question precision: asked when it should (`clarify`), stayed silent when it shouldn't (`answer`).
Uses a throwaway app DB so the glossary starts empty and nothing leaks between runs.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import agent, schema, store, warehouse  # noqa: E402
from app.config import settings  # noqa: E402


def _norm(rows):
    def cell(v):
        if isinstance(v, float):
            return f"{round(v, 2):.2f}"
        if isinstance(v, int):
            return f"{float(v):.2f}"
        return str(v)
    return sorted(tuple(sorted(cell(v) for v in r)) for r in rows)


async def main():
    settings.app_db_path = str(Path(tempfile.mkdtemp()) / "eval.db")
    store.init()
    schema.crawl()
    allowed = set(store.load_schema_docs())
    cases = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())["cases"]

    passed = 0
    for case in cases:
        events = await agent.collect(agent.new_conversation_id(), case["question"])
        asked = any(e["type"] == "question" for e in events)
        tables = [e for e in events if e["type"] == "table"]
        errors = [e["message"] for e in events if e["type"] == "error"]

        if case["kind"] == "clarify":
            ok = asked and not tables
            detail = "asked" if asked else "did NOT ask"
        else:
            gold = warehouse.run_query(case["expected_sql"], allowed)
            ok = (not asked) and bool(tables) and _norm(tables[-1]["rows"]) == _norm(gold["rows"])
            detail = "asked unnecessarily" if asked else ("matched gold" if ok else "result mismatch")
        if errors:
            ok, detail = False, f"error: {errors[0]}"
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] ({case['kind']}) {case['question']}  -> {detail}")

    print(f"\n{passed}/{len(cases)} passed")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    asyncio.run(main())
