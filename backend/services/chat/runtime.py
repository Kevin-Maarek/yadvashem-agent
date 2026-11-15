"""Execute a turn against durable, server-owned conversation state."""
import asyncio
from uuid import UUID, uuid4

from schemas.api import ChatRequest, ChatResult
from services.chat import conversations, search


async def run_turn(graph, body: ChatRequest) -> ChatResult:
    conversation_id = str(body.conversation_id) if body.conversation_id else (await asyncio.to_thread(conversations.create))["id"]
    request_id = str(body.request_id or uuid4())
    existing = await asyncio.to_thread(conversations.replay, conversation_id, request_id)
    if existing:
        return existing
    snapshot = await asyncio.to_thread(conversations.load, conversation_id)
    working = snapshot["state"]
    context = await asyncio.to_thread(search.context_images, body.source, working.get("image_ids", []))
    transcript = [{"sequence": turn["sequence"], "user": turn["message"][:350], "assistant": turn["result"]["text"][:450]}
                  for turn in snapshot["turns"]]
    state = await graph.ainvoke({"request": body, "working": working,
                                "transcript": transcript, "context": context})
    result = state["result"].model_copy(update={"conversation_id": UUID(conversation_id)})
    route = state["route"]
    next_working = {**working, "memory": state.get("memory", working.get("memory", "")),
                    "language": route.language}
    if route.action == "search" and not state.get("action_failed"):
        ids = [image.image_id for image in result.images]
        next_working.update(image_ids=ids, requirements=result.requirements, search_query=result.search_query,
                            focus_image_id=ids[0] if len(ids) == 1 else None)
        if "research" in next_working:
            next_working["research"] = {**next_working["research"], "active_photo_id": None, "active_case": None}
    elif route.action == "investigate":
        next_working["research"] = state.get("research_working", working.get("research", {}))
        if state.get("research_focus_image_id"):
            next_working["focus_image_id"] = state["research_focus_image_id"]
    elif route.action == "discuss" and route.image_index and not route.clarification:
        next_working["focus_image_id"] = context[route.image_index - 1].image_id
        if "research" in next_working:
            next_working["research"] = {**next_working["research"], "active_photo_id": None, "active_case": None}
    return await asyncio.to_thread(conversations.save, snapshot, request_id, body.message, result, next_working)

