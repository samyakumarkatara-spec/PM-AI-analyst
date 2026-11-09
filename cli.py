"""CLI for building and testing without the UI.

  python -m app.cli crawl
  python -m app.cli ask "How many users signed up last month?"
"""
import asyncio
import sys

from . import agent, schema, store


def cmd_crawl():
    store.init()
    docs = schema.crawl()
    print(f"Crawled {len(docs)} tables:")
    for k, d in sorted(docs.items()):
        print(f"  {k}  ({len(d['columns'])} cols, ~{d['row_estimate']} rows)")


async def cmd_ask(question: str):
    store.init()
    conv = agent.new_conversation_id()
    text = question
    while True:
        awaiting = False
        async for ev in agent.run_turn(conv, text):
            t = ev["type"]
            if t == "text":
                print(ev["delta"], end="", flush=True)
            elif t == "sql":
                print(f"\n\n[SQL] {ev.get('purpose', '')}\n{ev['sql']}" + (f"\n[ERROR] {ev['error']}" if ev.get("error") else ""))
            elif t == "table":
                print(f"[{ev['row_count']} rows] {ev['columns']}  first: {ev['rows'][:3]}")
            elif t == "glossary":
                print(f"[glossary saved] {ev['term']} = {ev['definition']}")
            elif t == "question":
                print(f"\n\n? {ev['question']}  ({ev['why']})")
                for i, o in enumerate(ev["options"], 1):
                    print(f"   {i}. {o}")
            elif t == "error":
                print(f"\n[error] {ev['message']}")
            elif t == "done":
                awaiting = ev["awaiting_answer"]
        print()
        if not awaiting:
            return
        text = input("> ")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "crawl":
        cmd_crawl()
    elif len(sys.argv) >= 3 and sys.argv[1] == "ask":
        asyncio.run(cmd_ask(" ".join(sys.argv[2:])))
    else:
        print(__doc__)
