"""API 路由：评审会话、SSE 事件流、健康检查与能力自省。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from reviewhive.agents.profiles import ALL_PROFILES
from reviewhive.core.schema import ReviewInput, StreamEvent


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health(request: Request) -> dict:
        deps = request.app.state.deps
        settings = request.app.state.settings

        async def _safe(coro):
            try:
                return await coro
            except Exception:
                return False

        llm_ok = await _safe(deps.llm.healthy())
        vision_ok = None
        if settings.models.vision.enabled:
            vision_ok = await _safe(deps.vision.healthy())
        qdrant_ok, es_ok = await asyncio.gather(
            asyncio.to_thread(_store_alive, deps.vectorstore.count),
            asyncio.to_thread(_store_alive, deps.keywordstore.count),
        )
        return {
            "llm": llm_ok,
            "vision": vision_ok,
            "qdrant": qdrant_ok,
            "elasticsearch": es_ok,
            "vision_enabled": settings.models.vision.enabled,
        }

    @router.post("/reviews")
    async def create_review(payload: ReviewInput, request: Request) -> dict:
        pipeline = request.app.state.pipeline
        store = request.app.state.store
        queues = request.app.state.event_queues
        tasks = request.app.state.background_tasks

        session_id = _new_session_id()
        store.create(session_id, payload.model_dump_json())
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        queues[session_id] = queue

        async def emit(event: StreamEvent) -> None:
            await queue.put(event)

        task = asyncio.create_task(pipeline.run(payload, emit, session_id=session_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"session_id": session_id}

    @router.get("/reviews/{session_id}/events")
    async def stream_events(session_id: str, request: Request):
        queues = request.app.state.event_queues
        store = request.app.state.store
        queue = queues.get(session_id)

        async def generator():
            if queue is None:
                session = store.get(session_id)
                if session and session["report"]:
                    yield _sse(StreamEvent(type="report", data=session["report"]))
                else:
                    yield _sse(StreamEvent(type="error", data={"message": "会话不存在或未开始"}))
                return
            while True:
                event = await queue.get()
                yield _sse(event)
                if event.type in ("report", "error"):
                    queues.pop(session_id, None)
                    return

        return StreamingResponse(generator(), media_type="text/event-stream")

    @router.get("/reviews/{session_id}")
    async def get_review(session_id: str, request: Request) -> dict:
        session = request.app.state.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    @router.get("/reviews")
    async def list_reviews(request: Request) -> list[dict]:
        return request.app.state.store.list_recent()

    @router.get("/agents")
    async def list_agents(request: Request) -> list[dict]:
        configured = request.app.state.settings.review.sub_agents
        agents = []
        for name, profile in ALL_PROFILES.items():
            agents.append(
                {
                    "name": profile.name,
                    "title": profile.title,
                    "goal": profile.goal,
                    "enabled": name in configured or name == "vision",
                }
            )
        return agents

    @router.get("/skills")
    async def list_skills(request: Request) -> list[dict]:
        return request.app.state.deps.standard_registry.describe()

    @router.get("/memories")
    async def list_memories(request: Request, type: str | None = None) -> list[dict]:
        bank = await request.app.state.deps.memory_bank()
        if not bank:
            raise HTTPException(404, "记忆系统未启用")
        memories = bank.list_active()
        if type:
            memories = [m for m in memories if m.type == type]
        return [m.model_dump() for m in memories]

    @router.post("/memories")
    async def add_memory(payload: dict, request: Request) -> dict:
        bank = await request.app.state.deps.memory_bank()
        if not bank:
            raise HTTPException(404, "记忆系统未启用")
        content = payload.get("content", "")
        if not content or len(content.strip()) < 4:
            raise HTTPException(400, "content 不能为空且至少 4 个字符")
        memory = bank.add(
            content=content,
            type_=payload.get("type", "lesson"),
            importance=payload.get("importance", 0.5),
        )
        if memory is None:
            return {"skipped": True, "reason": "duplicate"}
        return memory.model_dump()

    @router.delete("/memories/{memory_id}")
    async def delete_memory(memory_id: str, request: Request) -> dict:
        bank = await request.app.state.deps.memory_bank()
        if not bank:
            raise HTTPException(404, "记忆系统未启用")
        bank.remove(memory_id)
        return {"ok": True}

    @router.post("/memories/maintain")
    async def trigger_maintain(request: Request) -> dict:
        bank = await request.app.state.deps.memory_bank()
        if not bank:
            raise HTTPException(404, "记忆系统未启用")
        stats = bank.maintain()
        return stats

    return router


def _sse(event: StreamEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _new_session_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _store_alive(counter) -> bool:
    try:
        counter()
        return True
    except Exception:
        return False
