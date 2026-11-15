"""Person research with optional photo context and conversation-owned evidence."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
import re
from typing import Literal

from pydantic import ConfigDict, Field
from sqlalchemy import select

from core.config import settings
from core.errors import StageError
from core.text import lexical, signature_key
from db.models import Image, Record
from db.session import Session
from domain.catalog import catalog_fields, NAME_FIELD, TEXT_FIELDS
from integrations.names import Names, STOP_ERRORS, names_id, person_url
from integrations.yadvashem import clean
from prompts import load_prompt
from schemas.api import ChatResult, Citation, StrictModel
from schemas.research import ResearchCandidate, ResearchCitation, ResearchResult

log = logging.getLogger(__name__)


class NameParts(StrictModel):
    model_config = ConfigDict(json_schema_extra={"required": ["first_name", "last_name", "certain"]})
    first_name: str = Field("", max_length=250)
    last_name: str = Field("", max_length=250)
    certain: bool = False


class Comparison(StrictModel):
    candidate_field: str = Field(max_length=500)
    candidate_quote: str = Field(max_length=300)
    reference_field: str = Field(max_length=300)
    reference_quote: str = Field(max_length=300)
    relation: Literal["supports", "conflicts"]


class CandidateChoice(StrictModel):
    cluster_id: str
    comparisons: list[Comparison] = Field(default_factory=list, max_length=6)


class CandidateReview(StrictModel):
    choices: list[CandidateChoice] = Field(max_length=3)


class ResearchAnswer(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    cited_research_source_ids: list[str] = Field(min_length=1, max_length=12,
        description="Supporting research source IDs.")


def load_photo(source, *, photo_id=None, image_id=None, signature=None):
    """Resolve an active local photo without exposing raw source data to the browser."""
    with Session() as session:
        query = select(Record, Image.id).join(Image).where(Record.source == source, Image.active)
        if photo_id:
            query = query.where(Record.photo_id == photo_id)
        elif image_id:
            query = query.where(Image.id == image_id)
        elif signature:
            query = query.where(Record.signature_norm == signature_key(signature))
        else:
            return None
        row = session.execute(query.order_by(Image.position, Image.id)).first()
        if not row:
            return None
        record, image_number = row
        return {"photo_id": record.photo_id, "image_id": image_number, "title": record.title,
                "catalog_description": record.catalog_description, "source_url": record.source_url,
                "metadata_fields": record.metadata_fields, "provenance": record.provenance,
                "raw_details": record.raw_details, "raw_search": record.raw_search,
                "fields": catalog_fields(record)}


def photo_people(photo):
    """Keep each name paired with its own URL, even when normalized names collapse."""
    people, seen = [], set()

    def add(name, cluster_id, field):
        key = (lexical(name), cluster_id)
        if name and key not in seen:
            seen.add(key)
            people.append({"name": name, "cluster_id": cluster_id, "source_field": field})

    def walk(value, path, associated=False):
        if isinstance(value, dict):
            associated = associated or value.get("title") == "cluster" or value.get("id") == "cluster"
            if associated and isinstance(value.get("value"), str):
                add(clean(value["value"]), names_id(value.get("url")), path + ".value")
            for key, item in value.items():
                if isinstance(item, (list, dict)):
                    walk(item, f"{path}.{key}", associated)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", associated)

    walk(photo.get("raw_details"), "raw_details")
    walk(photo.get("raw_search"), "raw_search")
    fields = photo["fields"]
    contributors = photo.get("provenance", {}).get("names_norm", {}).get("contributors", [])
    for item in contributors:
        field, name = item.get("source_field", ""), item.get("value", "")
        if (isinstance(name, str) and (field in TEXT_FIELDS or NAME_FIELD.fullmatch(field))
                and lexical(name) and lexical(name) in lexical(fields.get(field, ""))):
            # A normalized extraction of an already linked name is not another person.
            if not any(lexical(person["name"]) == lexical(name) for person in people):
                add(name, None, field)
    # Associated cluster strings also work on older records without enrichment.
    for field, name in fields.items():
        if NAME_FIELD.fullmatch(field) and not any(lexical(p["name"]) == lexical(name) for p in people):
            add(name, None, field)
    return people


def valid_name_parts(parts: NameParts, name: str) -> bool:
    if not parts.certain:
        return False
    # Every proposed component must occur in the name, with no discarded name tokens.
    tokens = lambda value: re.findall(r"[^\W_]+", lexical(value))
    return bool(tokens(parts.first_name) or tokens(parts.last_name)) and (
        sorted(tokens(parts.first_name) + tokens(parts.last_name)) == sorted(tokens(name)))


def punctuation_variant(first, last):
    return tuple(re.sub(r"['׳’]+$", "", " ".join(value.split())).rstrip() for value in (first, last))


def selection_index(message):
    """Resolve literal references to the last server-owned numbered person list."""
    message = message.strip().casefold().rstrip(".!?")
    if re.fullmatch(r"[1-9][0-9]?", message):
        return int(message)
    match = re.search(r"\b(?:candidate|person)\s*#?\s*([1-9][0-9]?)\b", message)
    if match:
        return int(match[1])
    for index, word in enumerate(("first", "second", "third"), 1):
        if re.search(rf"\b(?:the\s+)?{word}\s+(?:one|candidate|person)\b", message):
            return index
    for index, word in enumerate(("הראשון", "השני", "השלישי"), 1):
        if message in (word, "המועמד " + word, "האדם " + word):
            return index
    return None


def references(photo, case):
    result = {key: value for key, value in (photo["fields"] if photo else {}).items()
              if key in TEXT_FIELDS or NAME_FIELD.fullmatch(key) or key.startswith("metadata_fields.place")}
    for index, clue in enumerate(case.get("clues", [])):
        result[f"user_clues[{index}]"] = clue
    return result


def bounded_fields(fields, *, budget=6000):
    # Truncation is explicit; never feed source-sized strings into a model context.
    result, remaining = {}, budget
    for key, value in fields.items():
        if not isinstance(value, str):
            continue
        available = min(700, remaining - len(key) - 30)
        if available < 1 or len(result) >= 60:
            result["_truncated"] = "Additional source fields omitted from this context."
            break
        result[key] = value[:available] + (" [truncated]" if len(value) > available else "")
        remaining -= len(key) + len(result[key])
    return result


def card_fields(card):
    return {"name": card["name"], "birth_year": card.get("birth_year") or "",
            "places": "; ".join(card.get("places", [])), "fate": card.get("fate") or "",
            "sources": "; ".join(card.get("sources", []))}


def candidate_fields(card, case, *, budget=1500):
    fields = card_fields(card)
    cached = case.get("profiles", {}).get(card["cluster_id"], {})
    for source in cached.get("sources", []):
        for key, value in source["fields"].items():
            fields[f"{source['source_id']}/{key}"] = value
    return bounded_fields(fields, budget=budget)


def grounded_choices(review, cards, photo, case):
    by_id = {card["cluster_id"]: card for card in cards}
    refs = references(photo, case)
    chosen, used = [], set()
    for choice in review.choices:
        if choice.cluster_id not in by_id or choice.cluster_id in used:
            continue
        card = deepcopy(by_id[choice.cluster_id])
        fields = candidate_fields(card, case)
        supports = []
        for comparison in choice.comparisons:
            candidate_quote, reference_quote = lexical(comparison.candidate_quote), lexical(comparison.reference_quote)
            if (candidate_quote and reference_quote
                    and candidate_quote in lexical(fields.get(comparison.candidate_field, ""))
                    and reference_quote in lexical(refs.get(comparison.reference_field, ""))):
                supports.append(comparison.model_dump())
        # Ungrounded reasoning cannot move a candidate ahead of source order.
        if supports:
            card["comparisons"] = supports
            chosen.append(card)
            used.add(choice.cluster_id)
    return (chosen + [deepcopy(c) for c in cards if c["cluster_id"] not in used])[:3]


async def review_candidates(ollama, cards, photo, case):
    if len(cards) <= 1:
        return deepcopy(cards)
    try:
        review = await ollama.structured(settings.chat_model, [
            {"role": "system", "content": load_prompt("research.compare.txt")},
            {"role": "user", "content": json.dumps({"references": bounded_fields(references(photo, case)),
                "candidates": [{"cluster_id": c["cluster_id"], "fields": candidate_fields(c, case)} for c in cards]}, ensure_ascii=False)},
        ], CandidateReview, interactive=True, num_predict=2500, num_ctx=32768)
        return grounded_choices(review, cards, photo, case)
    except StageError:
        return deepcopy(cards[:3])


def source_list(case, language):
    sources = []
    for candidate in case.get("displayed", []):
        number = candidate["cluster_id"]
        if number:
            sources.append({"source_id": f"names:{language}:{number}:card", "title": candidate["name"],
                "url": person_url(number, language), "fields": card_fields(candidate)})
            sources.extend(case.get("profiles", {}).get(number, {}).get("sources", []))
    return list({s["source_id"]: s for s in sources}.values())


def candidate_text(candidate, index, english):
    bits = [candidate["name"]]
    if candidate.get("birth_year"):
        bits.append(("born " if english else "שנת לידה: ") + candidate["birth_year"])
    if candidate.get("places"):
        bits.append(("places recorded: " if english else "מקומות ברשומה: ") + "; ".join(candidate["places"]))
    if candidate.get("fate"):
        bits.append(candidate["fate"])
    return f"{index}. " + " — ".join(bits)


def factual_fallback(sources, english):
    labels = {
        "YSODB": ("Birth year", "שנת לידה"), "YSPB": ("Birthplace", "מקום לידה"),
        "YSFTHN": ("Father's name", "שם האב"), "YSSPN": ("Spouse's name", "שם בן/בת הזוג"),
        "PROF": ("Occupation", "מקצוע"), "YSPLD": ("Death place", "מקום מוות"),
        "YSDD": ("Death date", "תאריך מוות"), "YSSBN": ("Testimony submitter", "מוסר/ת העדות"),
        "YSFR": ("Submitter's relationship", "קרבת מוסר/ת העדות"),
    }
    lines = []
    for source in sources:
        if ":record:" not in source["source_id"]:
            continue
        values = []
        for field, value in source["fields"].items():
            code = field.rsplit(".", 1)[-1]
            if code in labels:
                values.append(f"{labels[code][0 if english else 1]}: {value[:200]}")
        if values:
            lines.append(source["title"] + ":\n" + "\n".join(values[:10]))
    return "\n\n".join(lines[:3])


def unsupported_association(text, basis):
    if basis == "source_linked":
        return False
    # A candidate's own Photos records need not contain the target photograph.
    return bool(re.search(r"(?:records?|candidates?)\s+(?:are\s+)?(?:linked|associated|connected)\s+"
                          r"(?:to|with)\s+(?:this|the|your)\s+photo(?:graph)?|"
                          r"(?:this|the|your)\s+photo(?:graph)?\s+(?:is|has been)\s+(?:linked|associated|connected)|"
                          r"(?:הרשומות|המועמדים|הרשומה)\s+(?:האלה\s+)?מקושר(?:ים|ות|ת)?\s+(?:לתצלום|לתמונה)", text, re.I))


async def finish(ollama, photo, case, route, state, *, message="", summarize=False):
    english = route.language == "en"
    sources = source_list(case, route.language)
    cited = {source["source_id"] for source in sources if source["source_id"].endswith(":card")}
    text = message
    if summarize and sources:
        try:
            response = await ollama.structured(settings.chat_model, [
                {"role": "system", "content": load_prompt("research.answer.txt")},
                {"role": "user", "content": json.dumps({"request": state["request"].message,
                    "response_language": route.language, "basis": case.get("basis", "possible"),
                    "scope": "photo" if photo else "person",
                    "photo_catalog": bounded_fields(references(photo, case), budget=4000),
                    "candidates": case.get("displayed", []),
                    "sources": [{**{k: v for k, v in source.items() if k != "fields"},
                                 "fields": bounded_fields(source["fields"], budget=max(500, 16000 // len(sources)))} for source in sources]}, ensure_ascii=False)},
            ], ResearchAnswer, interactive=True, num_predict=1800, num_ctx=16384)
            valid = {source["source_id"] for source in sources}
            # Fail closed on fabricated citations or unreferenced biographical prose.
            if (response.cited_research_source_ids and set(response.cited_research_source_ids) <= valid
                    and not unsupported_association(response.text, case.get("basis"))):
                text = "\n\n".join(part for part in (text, response.text) if part)
                cited.update(response.cited_research_source_ids)
        except StageError:
            pass
        if not text:
            text = ("The following source records are available. Some details may be missing or conflicting."
                    if english else "רשומות המקור הבאות זמינות. חלק מהפרטים עשויים להיות חסרים או סותרים.")
            # The card fields below remain a factual fallback if the model fails.
            cited.update(source["source_id"] for source in sources)
            fallback = factual_fallback(sources, english)
            if fallback:
                text += "\n\n" + fallback
    basis = case.get("basis", "possible")
    if case.get("displayed"):
        labels = {
            "source_linked": ("Yad Vashem links this photograph to the following person record(s). This does not locate a person within the image.",
                              "יד ושם מקשר את התצלום לרשומות האנשים הבאות. הקישור אינו מזהה את מיקומם בתמונה."),
            "user_selected": ("You selected this candidate for investigation; the photograph-to-person identification remains unverified.",
                              "בחרת במועמד הזה להמשך הבדיקה; הזיהוי שלו עם האדם בתצלום עדיין אינו מאומת."),
            "possible": ("Possible name matches; none is established as the person in the photograph.",
                         "התאמות אפשריות לפי שם; לא נקבע שאחת מהן היא האדם בתצלום."),
        }
        if not photo:
            labels.update(
                possible=("Possible matches for the supplied name; identity is not confirmed by a name match alone.",
                          "התאמות אפשריות לשם שנמסר; התאמת שם בלבד אינה מאמתת זהות."),
                user_selected=("You selected this candidate for further research; the name match alone does not confirm identity.",
                               "בחרת במועמד הזה להמשך המחקר; התאמת השם לבדה אינה מאמתת זהות."))
        text = "\n\n".join(part for part in (labels[basis][0 if english else 1], text,
            "\n".join(candidate_text(c, i, english) for i, c in enumerate(case["displayed"], 1))) if part)
        if basis == "possible" and case.get("status") != "choose_person":
            text += ("\n\nDo you know approximately when this person was born, where they lived, or a relative's name?"
                     if english else "\n\nהאם ידועים לך שנת לידה משוערת, מקום מגורים או שם של קרוב משפחה?")
    if case.get("total") is not None and case.get("displayed") and basis == "possible":
        text += ((f"\n\nRetrieved {case.get('reviewed', 0)} candidate cards out of {case['total']} reported results; "
                  f"showing {len(case['displayed'])} candidates.") if english else
                 f"\n\nהתקבלו {case.get('reviewed', 0)} כרטיסי מועמדים מתוך {case['total']} תוצאות שדווחו; "
                 f"מוצגים {len(case['displayed'])} מועמדים.")
    if case.get("has_more"):
        text += ("\n\nOther results have not been reviewed; you can ask for more candidates." if english else
                 "\n\nתוצאות נוספות טרם נבדקו; אפשר לבקש מועמדים נוספים.")
    if case.get("errors"):
        text += ("\n\nSome source requests failed; the answer uses the evidence that was retrieved."
                 if english else "\n\nחלק מבקשות המקור נכשלו; התשובה מבוססת על המידע שהתקבל.")
    result = ChatResult(text=text, images=[],
        citations=[Citation(photo_id=photo["photo_id"], title=photo["title"], url=photo["source_url"])] if photo and photo.get("source_url") else [],
        requirements=state["requirements"], search_query=state["query"] or None, semantic_available=True,
        action="investigate", research=ResearchResult(photo_id=photo["photo_id"] if photo else None, status=case.get("status", "unresolved"),
            candidates=[ResearchCandidate.model_validate(c) for c in case.get("displayed", [])],
            total=case.get("total"), reviewed=case.get("reviewed", 0), has_more=case.get("has_more", False)),
        research_citations=[ResearchCitation.model_validate(s) for s in sources if s["source_id"] in cited])
    return result


async def investigate(ollama, tools: Names, state):
    route, working = state["route"], state.get("working", {})
    english = route.language == "en"
    store = deepcopy(working.get("research", {"cases": {}}))
    store.setdefault("cases", {})
    context = state.get("context", [])
    name = route.person_name.strip()
    supplied_name = name if name and lexical(name) in lexical(state["request"].message) else ""
    active_key = store.get("active_case") or str(store.get("active_photo_id"))
    explicit_photo = bool(route.photo_id or route.signature or route.image_index)
    standalone = not explicit_photo and route.research_scope != "photo" and (
        route.research_scope == "person"
        or supplied_name and route.research_operation == "start"
        or active_key.startswith("person:"))
    target = {}
    if route.photo_id:
        target["photo_id"] = route.photo_id
    elif route.signature:
        target["signature"] = route.signature
    elif route.image_index:
        target["image_id"] = context[route.image_index - 1].image_id
    elif not standalone and working.get("focus_image_id"):
        target["image_id"] = working["focus_image_id"]
    elif not standalone and len(context) == 1:
        target["image_id"] = context[0].image_id
    elif not standalone and store.get("active_photo_id") and not context:
        target["photo_id"] = store["active_photo_id"]
    photo = await asyncio.to_thread(load_photo, state["request"].source, **target) if target else None
    # A supplied name can start research even when no local photo exists. An explicit
    # photo reference must still resolve, rather than silently changing the request.
    if not photo and not target and supplied_name and route.research_scope != "photo":
        standalone = True
    if not photo and not standalone:
        result = ChatResult(text="Which photograph should I investigate? Please give its result number or photo ID."
            if english else "איזה תצלום לבדוק? אפשר לציין את מספר התוצאה או מזהה התצלום.",
            images=[], citations=[], semantic_available=True, action="investigate",
            requirements=state["requirements"], search_query=state["query"] or None)
        return {"result": result, "research_working": store}
    if standalone:
        if supplied_name and (route.research_operation == "start" or not active_key.startswith("person:")):
            key = "person:" + lexical(supplied_name)
        elif active_key.startswith("person:"):
            key = active_key
        else:
            result = ChatResult(text="What name should I search for? You can provide a first name, a last name, or both."
                if english else "איזה שם לחפש? אפשר לציין שם פרטי, שם משפחה או את שניהם.",
                images=[], citations=[], semantic_available=True, action="investigate",
                requirements=state["requirements"], search_query=state["query"] or None)
            return {"result": result, "research_working": store}
    else:
        key = str(photo["photo_id"])
    case = store["cases"].setdefault(key, {"profiles": {}, "clues": [], "displayed": [], "language": route.language})
    store.update(active_case=key, active_photo_id=photo["photo_id"] if photo else None)
    # Preserve selection/numbering when the user changes response language.
    if case.get("language") != route.language:
        languages = case.setdefault("profile_languages", {})
        languages[case["language"]] = case["profiles"]
        case["profiles"] = languages.pop(route.language, {})
        case["language"] = route.language
        case["errors"] = []
    case["updated_at"] = datetime.now(UTC).isoformat()
    if route.candidate_index is None and case.get("displayed") and not (route.photo_id or route.image_index or route.signature):
        if index := selection_index(state["request"].message):
            route = route.model_copy(update={"candidate_index": index, "research_operation": "select"})
    operation = route.research_operation
    if operation == "refresh":
        case["errors"] = []
    previous_query = deepcopy(case.get("query") or case.get("pending_query")) if operation == "refine" else None
    # An explicit partial name may change the requested field even when its text
    # is unchanged (for example, first name David -> surname David).
    if not photo and supplied_name and operation in ("start", "refine"):
        if previous_query:
            case["pending_query"] = previous_query
        else:
            case.pop("pending_query", None)
        case.update(displayed=[], query=None, selected_id=None)
    if operation == "refine" or route.person_name:
        clue = state["request"].message
        if clue not in case["clues"]:
            case["clues"] = [*case["clues"], clue][-12:]

    async def done(message="", summarize=False):
        result = await finish(ollama, photo, case, route, state, message=message, summarize=summarize)
        return {"result": result, "research_working": store,
                **({"research_focus_image_id": photo["image_id"]} if photo else {})}

    if operation == "select" and route.candidate_index is None:
        return await done("Which number in the person list do you mean?" if english else "לאיזה מספר ברשימת האנשים הכוונה?")

    people = photo_people(photo) if photo else ([case["subject"]] if case.get("subject") else [])
    if name and not any(lexical(name) == lexical(p["name"]) for p in people):
        # Only a name explicitly supplied by the user can supplement saved catalog names.
        if lexical(name) not in lexical(state["request"].message):
            # A planner spelling guess must not override an explicit saved source link.
            name = ""
        else:
            people.append({"name": name, "cluster_id": None, "source_field": "user_name"})
    if route.candidate_index is not None:
        displayed = case.get("displayed", [])
        if route.candidate_index > len(displayed):
            return await done("Please choose a number from the displayed people." if english else "אפשר לבחור מספר מרשימת האנשים שהוצגה?")
        selected = deepcopy(displayed[route.candidate_index - 1])
        if selected.get("cluster_id"):
            case.update(displayed=[selected], basis="source_linked" if selected.get("basis") == "source_linked" else "user_selected",
                        status="details", selected_id=selected["cluster_id"])
            case["displayed"][0]["basis"] = case["basis"]
        else:
            case["subject"] = {"name": selected["name"], "cluster_id": None, "source_field": selected.get("source_field", "catalog")}
            case.update(displayed=[], query=None)
    elif name:
        matching = [p for p in people if lexical(p["name"]) == lexical(name)]
        if len(matching) == 1 and case.get("subject") != matching[0]:
            case.update(subject=matching[0], displayed=[], query=None, selected_id=None)

    if not case.get("displayed") and not case.get("subject"):
        if not people:
            case["status"] = "needs_name"
            return await done("The saved catalog has no person link or extracted subject name. Do you know the person's name?"
                              if english else "בקטלוג השמור אין קישור לאדם או שם מזוהה של מצולם. האם ידוע לך שמו?")
        linked = [p for p in people if p["cluster_id"]]
        # Linked records take precedence; title spellings must not bypass these links.
        choices = linked or people
        if len(choices) > 1:
            case.update(displayed=[{**p, "basis": "source_linked" if p["cluster_id"] else "possible"} for p in choices],
                        basis="source_linked" if linked else "possible", status="choose_person")
            return await done("Which of these associated people should I investigate?" if english else "איזה מהאנשים המשויכים לתצלום לבדוק?")
        case["subject"] = choices[0]

    subject = case.get("subject", {})
    if subject.get("cluster_id") and not case.get("displayed"):
        case.update(displayed=[{**subject, "basis": "source_linked"}], basis="source_linked", status="details",
                    selected_id=subject["cluster_id"])

    if case.get("status") == "choose_person" and route.candidate_index is None and not name:
        return await done("Which person in the numbered list do you mean?" if english else "לאיזה אדם ברשימה הממוספרת הכוונה?")

    try:
        if case.get("status") == "no_matches" and operation == "start" and not name:
            return await done("No Names candidates were returned for the saved search terms. You can supply another name or ask to refresh."
                              if english else "לא התקבלו מועמדים למונחי החיפוש השמורים. אפשר לציין שם אחר או לבקש רענון.")
        if not case.get("displayed") or (case.get("query") and (operation == "more_candidates" or
                operation in ("refine", "refresh") and case.get("basis") != "user_selected")):
            if not case.get("query"):
                parts = await ollama.structured(settings.chat_model, [
                    {"role": "system", "content": load_prompt("research.name.txt")},
                    {"role": "user", "content": json.dumps({"name": subject.get("name", ""), "source_field": subject.get("source_field", ""),
                        "user_clarification": state["request"].message}, ensure_ascii=False)},
                ], NameParts, interactive=True, num_predict=350)
                if not valid_name_parts(parts, subject.get("name", "")):
                    case["status"] = "needs_name_parts"
                    return await done("Is this a first name, a last name, or both? You can search with either name on its own."
                                      if english else "האם זה שם פרטי, שם משפחה או שניהם? אפשר לחפש גם לפי אחד מהם בלבד.")
                case["query"] = {"first_name": parts.first_name.strip(), "last_name": parts.last_name.strip(),
                                 "client_id": tools.client_id, "page": 0}
                if previous_query and (supplied_name or case.get("pending_query")):
                    for field in ("first_name", "last_name"):
                        if not case["query"][field]:
                            case["query"][field] = previous_query[field]
                    case["subject"] = {"name": " ".join(case["query"][field] for field in ("first_name", "last_name") if case["query"][field]),
                                       "cluster_id": None, "source_field": "user_name"}
                case.pop("pending_query", None)
                case.update(attempts=[], cards=[], seen_ids=[], reviewed=0, has_more=False)
            query = case["query"]
            search_tools = Names(tools.client, client_id=query["client_id"])
            if operation == "more_candidates" and not case.get("has_more"):
                return await done("There are no further candidate pages in this search." if english else "אין עמודי מועמדים נוספים בחיפוש הזה.")
            if operation == "more_candidates" or not case.get("cards") or operation == "refresh":
                page = query["page"] + 1 if operation == "more_candidates" else 1
                response = await search_tools.search_people(query["first_name"], query["last_name"], route.language, page)
                case["attempts"].append({"first_name": query["first_name"], "last_name": query["last_name"],
                                         "page": page, "count": response.total})
                variant = punctuation_variant(query["first_name"], query["last_name"])
                original = (query["first_name"], query["last_name"])
                if (page == 1 and response.total == 0 and any(variant) and variant != original
                        and all(bool(before) == bool(after) for before, after in zip(original, variant))):
                    response = await search_tools.search_people(*variant, route.language, page)
                    case["attempts"].append({"first_name": variant[0], "last_name": variant[1], "page": page, "count": response.total})
                    query.update(first_name=variant[0], last_name=variant[1])
                cards = [c.model_dump() for c in response.cards if c.cluster_id not in case["seen_ids"] or page == 1]
                if page > 1 and response.cards and not cards:
                    raise StageError("names_repeated_page")
                case.update(cards=cards, total=response.total, has_more=bool(response.cards) and page * 25 < response.total,
                            raw_page=response.raw, basis="possible", status="candidates", selected_id=None)
                case["seen_ids"] = list(dict.fromkeys([*(case["seen_ids"] if page > 1 else []), *(c["cluster_id"] for c in cards)]))
                case["reviewed"] = len(case["seen_ids"])
                query["page"] = page
            cards = case.get("cards", [])
            if not cards:
                case.update(displayed=[], status="no_matches")
                return await done("No Names candidates were returned for these search terms. This does not establish that no record exists."
                                  if english else "לא התקבלו מועמדים במאגר השמות למונחי החיפוש האלה. אין בכך הוכחה שלא קיימת רשומה.")
            case["displayed"] = await review_candidates(ollama, cards, photo, case)
            for candidate in case["displayed"]:
                candidate["basis"] = "possible"

        for candidate in case["displayed"][:3]:
            number = candidate.get("cluster_id")
            if not number:
                continue
            cached = case["profiles"].get(number)
            if cached and operation not in ("more_details", "refresh"):
                continue
            details = await tools.get_person_details(number, route.language, cached=cached,
                more=operation == "more_details", refresh=operation == "refresh")
            case["profiles"][number] = details.model_dump()
            case["errors"] = list(dict.fromkeys([*case.get("errors", []), *details.errors]))
            if any(error.rsplit(": ", 1)[-1] in STOP_ERRORS for error in details.errors):
                break
        if case.get("basis") == "possible" and len(case["displayed"]) > 1:
            # Full-source comparisons may refine this shortlist without fetching new people.
            case["displayed"] = await review_candidates(ollama, case["displayed"], photo, case)
        return await done(summarize=True)
    except StageError as exc:
        log.warning("research_failed", extra={"metrics": {"code": exc.code, "operation": operation}})
        case["errors"] = list(dict.fromkeys([*case.get("errors", []), exc.code]))
        if exc.code in STOP_ERRORS:
            message = ("Names access is temporarily unavailable or cooling down. Please try again after access is restored."
                       if english else "הגישה למאגר השמות חסומה זמנית או בהמתנה. אפשר לנסות שוב לאחר חידוש הגישה.")
        else:
            message = ("I could not complete the Names lookup. This is a retrieval failure, not a finding of no matches."
                       if english else "לא ניתן להשלים את הבדיקה במאגר השמות. זו תקלה באחזור ולא תוצאה של היעדר התאמות.")
        return await done(message)
