import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, schema, store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="PM Analyst")


@app.on_event("startup")
def _startup():
    store.init()


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv_id = req.conversation_id or agent.new_conversation_id()

    async def stream():
        yield f"data: {json.dumps({'type': 'conversation', 'id': conv_id})}\n\n"
        async for ev in agent.run_turn(conv_id, req.message):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/glossary")
def glossary():
    return store.glossary_all()


@app.delete("/api/glossary/{term}")
def glossary_delete(term: str):
    store.glossary_delete(term)
    return {"ok": True}


@app.post("/api/schema/refresh")
def refresh_schema():
    docs = schema.crawl()
    return {"tables": sorted(docs)}


@app.get("/api/health")
def health():
    return {"ok": True, "tables": len(store.load_schema_docs())}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
