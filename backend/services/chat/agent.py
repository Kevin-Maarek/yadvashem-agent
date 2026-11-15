import json
import logging
import re
import time
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ConfigDict, Field

from services.chat import search
from services.chat import research
from integrations.names import Names
from integrations.yadvashem import photo_id as valid_photo_id
from services.chat.relevance import CANDIDATE_LIMIT, Review, accepted_ids, candidate_evidence
from integrations.models import Models
from integrations.ollama import Ollama
from core.config import settings
from schemas.api import ChatRequest, ChatResult, Citation, ImageResult, StrictModel
from core.errors import StageError
from prompts import load_prompt

log = logging.getLogger(__name__)


class Route(StrictModel):
    # Require every planner field in the model's output grammar. Unused references
    # are nullable; Python defaults still support application-created fallback plans.
    model_config = ConfigDict(json_schema_extra={
        "required": ["action", "query", "requirements", "photo_id", "signature", "image_index",
                     "clarification", "language", "research_operation", "candidate_index",
                     "person_name", "research_scope"],
    })

    action: Literal["search", "discuss", "investigate"]
    query: str = Field("", max_length=2400,
                       description="Standalone photo search text.")
    requirements: list[str] = Field(default_factory=list, max_length=12,
                                    description="Atomic photo search requirements.")
    photo_id: int | None = Field(None, gt=0, le=2**63 - 1,
        description="Photo collection ID.")
    signature: str | None = Field(None, min_length=1, max_length=120)
    image_index: int | None = Field(None, ge=1, le=15)
    clarification: str = Field("", max_length=500)
    language: Literal["en", "he"] = "he"
    research_operation: Literal["start", "refine", "select", "more_candidates", "more_details", "refresh"] = "start"
    candidate_index: int | None = Field(None, ge=1, le=100)
    person_name: str = Field("", max_length=250,
                            description="Person name for Names research.")
    research_scope: Literal["auto", "person", "photo"] = Field("auto",
        description="Person investigation scope.")


class Answer(StrictModel):
    text: str = Field(min_length=1, max_length=6000)
    cited_photo_ids: list[int] = Field(default_factory=list, max_length=15)
    memory: str = Field("", max_length=1800)


class State(TypedDict, total=False):
    request: ChatRequest
    route: Route
    requirements: list[str]
    query: str
    english: bool
    semantic_available: bool
    notices: list[str]
    images: list[ImageResult]
    record: object
    result: ChatResult
    working: dict
    transcript: list[dict]
    context: list[ImageResult]
    memory: str
    action_failed: bool
    relevance_evidence: list[dict]
    research_working: dict
    research_focus_image_id: int


def evidence(images: list[ImageResult], *, compact: bool = False) -> list[dict]:
    budget = max(180, (3000 if compact else 6000) // max(1, len(images)))
    return [{"position": index, "image_id": image.image_id, "photo_id": image.photo_id,
             "title": image.title[:160],
             "catalog": image.catalog_description[:budget // 2],
             "ai_visual_observations": (image.vision_description or "")[:budget // 2],
             **({} if compact else {"metadata": json.dumps(image.metadata, ensure_ascii=False)[:350]})}
            for index, image in enumerate(images, 1)]


def conversation_context(state: State) -> dict:
    working = state.get("working", {})
    research_state = working.get("research", {})
    case_key = research_state.get("active_case") or str(research_state.get("active_photo_id"))
    case = research_state.get("cases", {}).get(case_key, {})
    return {"memory": working.get("memory", ""),
            "recent_conversation": state.get("transcript", [])[-6:],
            "working_state": {key: working.get(key) for key in
                              ("search_query", "requirements", "focus_image_id", "language")},
            "current_results": evidence(state.get("context", []), compact=True),
            "person_investigation": {"photo_id": research_state.get("active_photo_id"),
                "scope": "person" if case_key.startswith("person:") else "photo",
                **{key: case.get(key) for key in ("status", "subject", "displayed", "query", "has_more", "basis")}},
            "request": state["request"].message}


def explicit_photo_reference(message: str) -> int | None:
    """Recover literal IDs when the planner chooses a lookup but omits its argument."""
    pattern = (r"(?:\bphoto(?:graph)?\s+(?:id\s*[:#]?|#)\s*|"
               r"\b(?:show|open|find)\s+(?:me\s+)?photo(?:graph)?\s+(?:id\s*[:#]?\s*)?|"
               r"(?:תמונה|תצלום)\s+(?:מספר|מזהה)\s*[:#]?\s*)([1-9][0-9]{0,18})\b")
    numbers = {int(match) for match in re.findall(pattern, message, re.I) if valid_photo_id(match)}
    return next(iter(numbers)) if len(numbers) == 1 else None


def build_graph(ollama: Ollama | Models, names: Names | None = None):
    router_prompt = load_prompt("chat.router.txt")
    relevance_prompt = load_prompt("chat.relevance.txt")
    answer_prompt = load_prompt("chat.answer.txt")

    async def route_turn(state: State):
        working = state.get("working", {})
        context = state.get("context", [])
        try:
            route = await ollama.structured(settings.chat_model, [
                {"role": "system", "content": router_prompt},
                {"role": "user", "content": json.dumps(conversation_context(state), ensure_ascii=False)},
            ], Route, interactive=True, num_predict=600)
        except StageError:
            language = working.get("language", "en")
            route = Route(action="discuss", language=language, clarification=(
                "I couldn't plan this request. Please try asking to find pictures or discuss the results."
                if language == "en" else
                "לא ניתן לתכנן את הבקשה כרגע. אפשר לנסות לבקש חיפוש תמונות או שיחה על התוצאות."))
        english = route.language == "en"
        if route.action in ("search", "investigate") and route.photo_id is None and route.signature is None:
            if number := explicit_photo_reference(state["request"].message):
                route = route.model_copy(update={"photo_id": number})
        if route.image_index and route.image_index > len(context):
            route = route.model_copy(update={"action": "discuss", "image_index": None,
                "clarification": "Which photo do you mean?" if english else "לאיזו תמונה הכוונה?"})
        if (route.photo_id is not None and route.signature is not None) or (
                route.action == "search" and not route.query.strip() and not route.photo_id
                and not route.signature and not any(item.strip() for item in route.requirements)):
            route = route.model_copy(update={"action": "discuss", "clarification":
                "Which pictures should I search for?" if english else "אילו תמונות לחפש?"})
        if route.clarification:
            route = route.model_copy(update={"action": "discuss"})
        if route.action == "search":
            requirements = list(dict.fromkeys(item.strip() for item in route.requirements if item.strip()))
            query = route.query.strip() or "; ".join(requirements)
            requirements = requirements or ([query] if query else [])
        else:
            query = working.get("search_query") or ""
            requirements = working.get("requirements", [])
        log.info("chat_action", extra={"metrics": {"action": route.action, "context_images": len(context)}})
        return {"route": route, "requirements": requirements, "query": query, "english": english,
                "semantic_available": True, "notices": [], "images": [], "action_failed": False,
                "relevance_evidence": []}

    async def search_pictures(state: State):
        notices = []
        route = state["route"]
        if route.photo_id is not None or route.signature is not None:
            images, record = search.details_lookup(state["request"], number=route.photo_id,
                                                  signature=route.signature,
                                                  candidate_limit=CANDIDATE_LIMIT if state["query"] else None)
            return {"images": images, "record": record}
        digest = vector = None
        try:
            digest = await ollama.digest(settings.embedding_model)
            vector = await ollama.embed(state["query"], interactive=True, model_digest=digest)
        except StageError:
            notices.append("Semantic search is unavailable. Using text search." if state["english"]
                           else "החיפוש הסמנטי אינו זמין. נעשה שימוש בחיפוש טקסט.")
        return {"images": search.hybrid(state["request"], state["query"], vector, digest,
                                        candidate_limit=CANDIDATE_LIMIT),
                "semantic_available": vector is not None, "notices": notices}

    async def investigate_person(state: State):
        started = time.monotonic()
        tools = Names(names.client) if names is not None else Names(ollama.client)
        result = await research.investigate(ollama, tools, state)
        log.info("person_investigation", extra={"metrics": {
            "operation": state["route"].research_operation,
            "status": result["result"].research.status if result["result"].research else "needs_context",
            "ms": round((time.monotonic() - started) * 1000)}})
        return result

    async def check_relevance(state: State):
        if state["action_failed"] or not state["query"]:
            return {}
        if not state["images"]:
            return {"record": None}
        candidates = candidate_evidence(state["images"])
        working = state.get("working", {})
        try:
            review = await ollama.structured(settings.chat_model, [
                {"role": "system", "content": relevance_prompt},
                {"role": "user", "content": json.dumps({
                    "request": state["request"].message, "query": state["query"],
                    "requirements": [{"index": index, "text": requirement}
                                     for index, requirement in enumerate(state["requirements"])],
                    "previous_search": working.get("search_query"),
                    "recent_conversation": [
                        {"user": turn.get("user", "")[:200],
                         "assistant": turn.get("assistant", "")[:200]}
                        for turn in state.get("transcript", [])[-2:]],
                    "candidates": candidates,
                }, ensure_ascii=False)},
            ], Review, interactive=True, num_predict=10000, num_ctx=32768)
            accepted = accepted_ids(review, candidates, state["requirements"])
        except StageError as exc:
            log.warning("relevance_check_failed", extra={"metrics": {"code": exc.code}})
            notice = ("I couldn't verify the relevance of the search results. Please try again."
                      if state["english"] else
                      "לא ניתן לבדוק כרגע את הרלוונטיות של תוצאות החיפוש. אפשר לנסות שוב.")
            return {"images": [], "record": None, "action_failed": True,
                    "notices": [*state["notices"], notice]}
        images = search.select_results(state["request"], [
            image for image in state["images"] if image.image_id in accepted])
        log.info("relevance_check", extra={"metrics": {"candidates": len(candidates),
            "supported": len(accepted), "displayed": len(images)}})
        displayed = {image.image_id for image in images}
        return {"images": images, "record": state.get("record") if images else None,
                "relevance_evidence": [
            {"image_id": decision.image_id, **support.model_dump()}
            for decision in review.decisions if decision.image_id in displayed
            for support in decision.support]}

    async def answer(state: State):
        route = state["route"]
        memory = state.get("working", {}).get("memory", "")
        if route.clarification:
            return {"memory": memory, "result": ChatResult(text=route.clarification,
                images=[], citations=[], requirements=state["requirements"], search_query=state["query"] or None,
                semantic_available=True, action="discuss")}
        images = state["images"]
        context = images if route.action == "search" else state.get("context", [])
        focused = None
        if route.action == "discuss":
            focused = (context[route.image_index - 1].image_id if route.image_index else
                       state.get("working", {}).get("focus_image_id"))
        record = state.get("record")
        record_evidence = ({"photo_id": record.photo_id, "title": record.title,
                            "catalog": record.catalog_description[:3000]} if record else None)
        try:
            response = await ollama.structured(settings.chat_model, [
                {"role": "system", "content": answer_prompt},
                {"role": "user", "content": json.dumps({
                    **{key: value for key, value in conversation_context(state).items() if key != "current_results"},
                    "action": route.action, "action_failed": state["action_failed"],
                    "search_query": state["query"], "result_count": len(context),
                    "response_language": route.language, "focus_image_id": focused,
                    "evidence": evidence(context), "record": record_evidence,
                    "relevance_evidence": state.get("relevance_evidence", []),
                    "requirements": state["requirements"], "notices": state["notices"],
                }, ensure_ascii=False)},
            ], Answer, interactive=True, num_predict=1800)
            text = response.text
            memory = response.memory or memory
            cited = set(response.cited_photo_ids)
        except StageError:
            cited = {image.photo_id for image in images}
            if images:
                top = images[0]
                text = ((f"Showing {len(images)} images. Catalog — " if state["english"]
                         else f"מוצגות {len(images)} תמונות. קטלוג — ") + top.title + "\n" + top.catalog_description)
                if top.vision_description:
                    text += ("\nAI visual observations — " if state["english"] else "\nתצפיות חזותיות שנוצרו ב־AI — ") + top.vision_description
            elif record:
                text = record.title + "\n" + record.catalog_description
                cited.add(record.photo_id)
            elif state["action_failed"] or route.action == "discuss":
                text = "I couldn't complete this request. Please try again." if state["english"] else "לא ניתן להשלים את הבקשה כרגע. אפשר לנסות שוב."
            else:
                text = ("I couldn't find images supported by the available descriptions in the current index."
                        if state["english"] else
                        "לא נמצאו במאגר הנוכחי תמונות שהתיאורים הזמינים שלהן תומכים בבקשה.")
        citations = {image.photo_id: Citation(photo_id=image.photo_id, title=image.title, url=image.source_url)
                     for image in context if image.source_url and image.photo_id in cited}
        if record and record.source_url and record.photo_id in cited:
            citations[record.photo_id] = Citation(photo_id=record.photo_id, title=record.title, url=record.source_url)
        if state["notices"]:
            text = "\n".join(state["notices"]) + "\n\n" + text
        return {"memory": memory, "result": ChatResult(text=text, images=images,
            citations=list(citations.values()), requirements=state["requirements"], search_query=state["query"] or None,
            semantic_available=state["semantic_available"], notices=state["notices"], action=route.action)}

    graph = StateGraph(State)
    graph.add_node("route_turn", route_turn)
    graph.add_node("search", search_pictures)
    graph.add_node("investigate", investigate_person)
    graph.add_node("check_relevance", check_relevance)
    graph.add_node("answer", answer)
    graph.add_edge(START, "route_turn")
    graph.add_conditional_edges("route_turn", lambda state: state["route"].action,
                               {"search": "search", "discuss": "answer", "investigate": "investigate"})
    graph.add_edge("investigate", END)
    graph.add_edge("search", "check_relevance")
    graph.add_edge("check_relevance", "answer")
    graph.add_edge("answer", END)
    return graph.compile()
