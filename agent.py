"""The agent loop. Stateless between HTTP requests: all state lives in the stored
message list, which is how ask_user can pause a turn and resume it later.

Flow for a clarification:
  assistant emits tool_use(ask_user) -> we save messages and end the stream (awaiting_answer=True)
  user's next message arrives       -> we wrap it as the tool_result for that tool_use and continue
"""
import asyncio
import uuid
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from . import prompts, store
from .config import settings
from .tools import TOOLS, ToolRunner

_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    return _client


def _clean_blocks(content) -> list[dict]:
    out = []
    for b in content:
        if b.type == "text":
            if b.text.strip():
                out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def _pending_question(messages: list[dict]) -> list[dict] | None:
    """tool_use blocks of a trailing assistant message that contains ask_user, if unanswered."""
    if not messages or messages[-1]["role"] != "assistant":
        return None
    blocks = [b for b in messages[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    return blocks if any(b["name"] == "ask_user" for b in blocks) else None


def _clarifications_this_question(messages: list[dict]) -> int:
    """ask_user calls since the last plain-text user message (= the original question)."""
    n = 0
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], str):
            break
        if m["role"] == "assistant":
            n += sum(1 for b in m["content"] if b.get("type") == "tool_use" and b["name"] == "ask_user")
    return n


async def run_turn(conversation_id: str, user_text: str) -> AsyncIterator[dict]:
    messages = store.load_messages(conversation_id)

    pending = _pending_question(messages)
    if pending:
        results = []
        for b in pending:
            if b["name"] == "ask_user":
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": user_text})
            else:
                results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                "content": "Skipped: the assistant asked the user a question first. Re-run if still needed."})
        messages.append({"role": "user", "content": results})
    else:
        messages.append({"role": "user", "content": user_text})

    system = [{"type": "text", "text": prompts.build_system(), "cache_control": {"type": "ephemeral"}}]
    runner = ToolRunner(conversation_id)

    try:
        for _ in range(settings.max_agent_steps):
            async with client().messages.stream(
                model=settings.anthropic_model, max_tokens=4096, system=system,
                tools=TOOLS, messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "delta": event.text}
                final = await stream.get_final_message()

            blocks = _clean_blocks(final.content)
            messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no response)"}]})

            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            if final.stop_reason != "tool_use" or not tool_uses:
                store.save_messages(conversation_id, messages)
                yield {"type": "done", "awaiting_answer": False}
                return

            ask = next((b for b in tool_uses if b["name"] == "ask_user"), None)
            if ask and _clarifications_this_question(messages) <= settings.max_clarification_rounds:
                store.save_messages(conversation_id, messages)
                q = ask["input"]
                yield {"type": "question", "question": q.get("question", ""), "options": q.get("options", []),
                       "why": q.get("why_it_matters", "")}
                yield {"type": "done", "awaiting_answer": True}
                return

            results = []
            for b in tool_uses:
                if b["name"] == "ask_user":  # cap exceeded
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                    "content": "Clarification limit reached. Proceed with your best assumption and state it clearly."})
                    continue
                content, is_error = await asyncio.to_thread(runner.execute, b["name"], b["input"])
                for ev in runner.drain():
                    yield ev
                r = {"type": "tool_result", "tool_use_id": b["id"], "content": content}
                if is_error:
                    r["is_error"] = True
                results.append(r)
            messages.append({"role": "user", "content": results})

        store.save_messages(conversation_id, messages)
        yield {"type": "error", "message": "Stopped after too many steps. Try a more specific question."}
        yield {"type": "done", "awaiting_answer": False}
    except Exception as e:
        # Don't persist a half-finished tool exchange that would poison the history.
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        yield {"type": "done", "awaiting_answer": False}


async def collect(conversation_id: str, user_text: str) -> list[dict]:
    return [e async for e in run_turn(conversation_id, user_text)]


def new_conversation_id() -> str:
    return uuid.uuid4().hex[:12]
