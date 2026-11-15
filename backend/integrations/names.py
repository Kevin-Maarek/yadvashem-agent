"""Read-only Names tools. Search query context belongs to one conversation search."""
import logging
import re
import time
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from core.errors import StageError
from integrations.yadvashem import COLLECTIONS, bounded_request, clean, photo_id

log = logging.getLogger(__name__)
PAGE_SIZE = 25
STOP_ERRORS = {"source_forbidden", "source_rate_limited", "source_cooldown"}


def names_id(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "collections.yadvashem.org":
            return None
    match = re.fullmatch(r"/(?:he|en)/names/([1-9][0-9]{0,18})/?", parsed.path)
    return match[1] if match and photo_id(match[1]) and not parsed.query and not parsed.fragment else None


def person_url(cluster_id: str, language: str) -> str:
    return f"{COLLECTIONS}/{language}/names/{cluster_id}"


class PersonCard(BaseModel):
    cluster_id: str
    name: str
    first_name: str = ""
    last_name: str = ""
    birth_year: str | None = None
    places: list[str] = Field(default_factory=list)
    fate: str | None = None
    sources: list[str] = Field(default_factory=list)


class PeoplePage(BaseModel):
    cards: list[PersonCard]
    total: int
    page: int
    raw: dict


class PersonDetails(BaseModel):
    cluster_id: str
    sources: list[dict] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    attempted_records: list[str] = Field(default_factory=list)


class Names:
    def __init__(self, client: httpx.AsyncClient, *, client_id: str | None = None):
        self.client = client
        self.client_id = client_id or str(uuid4())
        self.headers = {"x-client-id": self.client_id}

    async def search_people(self, first_name: str, last_name: str, language: Literal["he", "en"],
                            page: int = 1) -> PeoplePage:
        first_name, last_name = first_name.strip(), last_name.strip()
        if not (first_name or last_name) or page < 1:
            raise StageError("names_search_terms")
        # Input spelling determines index fields; response language is independent.
        field_lang = "he" if re.search(r"[א-ת]", first_name + last_name) else "en"
        query = [{"fieldsNames": fields, "valueToSearch": value,
                  "searchType": "yvSynonym", "useLang": False, "queryOperator": 1}
                 for fields, value in (
                     ([f"first_name_search_{field_lang}"], first_name),
                     ([f"last_name_search_{field_lang}", f"maiden_name_search_{field_lang}"], last_name))
                 if value]
        started = time.monotonic()
        # Always rebuild, including for pagination: upstream context can expire.
        await bounded_request(self.client, "POST", f"{COLLECTIONS}/api/Search/BuidSpesificResultsQuery",
                              params={"tabName": "victim"}, json=query, headers=self.headers,
                              expected_empty=True, timeout=30)
        data = await bounded_request(self.client, "POST", f"{COLLECTIONS}/api/Search/GetDataResultsQuery",
            params={"valueToSearch": "null", "pageNumber": page, "pageSize": PAGE_SIZE,
                    "lang": language, "cardType": "namesCard", "tabName": "victim"},
            json={"queryOperator": 0, "filters": {"logicOperator": 0, "filters": [
                {"fieldName": "data_bank", "filterType": 0, "filterOperator": 0, "values": ["names"]}]},
                  "currentTab": {"id": "tab_type", "value": "names"}}, headers=self.headers, timeout=30)
        raw_cards, count = data.get("cards"), data.get("count")
        if not isinstance(raw_cards, list) or len(raw_cards) > PAGE_SIZE or type(count) is not int or count < 0:
            raise StageError("names_search_schema")
        cards, seen = [], set()
        for item in raw_cards:
            if not isinstance(item, dict) or photo_id(item.get("id")) is None:
                raise StageError("names_card_schema")
            number = str(item["id"])
            if number in seen:
                continue
            seen.add(number)
            places, related = item.get("placesTags") or [], item.get("relatedList") or []
            if not isinstance(places, list) or not isinstance(related, list):
                raise StageError("names_card_schema")
            cards.append(PersonCard(cluster_id=number, name=clean(item.get("title")) or number,
                first_name=clean(item.get("firstName")), last_name=clean(item.get("lastName")),
                birth_year=clean(item.get("birthYear")) or None,
                places=[clean(place) for place in places if clean(place)], fate=clean(item.get("fate")) or None,
                sources=[clean(entry.get("value")) for entry in related if isinstance(entry, dict) and clean(entry.get("value"))]))
        log.info("names_search", extra={"metrics": {"page": page, "candidates": len(cards), "total": count,
                                                   "ms": round((time.monotonic() - started) * 1000)}})
        return PeoplePage(cards=cards, total=count, page=page, raw=data)

    async def get_person_details(self, cluster_id: str, language: Literal["he", "en"], *,
                                 cached: dict | None = None, more: bool = False,
                                 refresh: bool = False) -> PersonDetails:
        if photo_id(cluster_id) is None:
            raise StageError("names_person_id")
        result = PersonDetails.model_validate(cached) if cached and not refresh else PersonDetails(cluster_id=cluster_id)
        if result.cluster_id != cluster_id:
            raise StageError("names_person_id")
        url = person_url(cluster_id, language)
        stopped = False

        async def fetch(endpoint, params, key):
            nonlocal stopped
            if stopped:
                return None
            try:
                value = await bounded_request(self.client, "GET", f"{COLLECTIONS}/api/Names/{endpoint}",
                                              params=params, record_lookup=True, timeout=30)
                result.raw[key] = value
                return value
            except StageError as exc:
                log.warning("names_details_partial", extra={"metrics": {"endpoint": endpoint, "code": exc.code}})
                if exc.code in STOP_ERRORS:
                    stopped = True
                result.errors.append(f"{endpoint}: {exc.code}")
                return None

        def add_source(source_id, title, fields):
            result.sources.append({"source_id": source_id, "title": title, "url": url,
                                   "fields": fields, "retrieved_at": datetime.now(UTC).isoformat()})

        base = {"id": cluster_id, "lang": language}
        if "cluster" not in result.raw:
            cluster = await fetch("GetClusterDetails", base, "cluster")
            if cluster:
                if str(cluster.get("clusterId")) != cluster_id or not isinstance(cluster.get("shortClusters"), list):
                    result.raw.pop("cluster", None)
                    raise StageError("names_cluster_schema")
                fields = {"title": clean(cluster.get("title")), "fate": clean(cluster.get("tag"))}
                for index, record in enumerate(cluster["shortClusters"]):
                    if isinstance(record, dict):
                        for key in ("firstName", "lastName", "birthYear", "placeOfResidence", "source"):
                            if value := clean(record.get(key)):
                                fields[f"records[{index}].{key}"] = value
                if cluster.get("map"):
                    # Keep place roles, not a single inferred photograph location.
                    fields["map"] = str(cluster["map"])
                add_source(f"names:{language}:{cluster_id}:cluster", fields["title"] or cluster_id, fields)
        if "story" not in result.raw and (not cached or refresh):
            story = await fetch("GetStory", base, "story")
            if story and isinstance(story.get("story"), str):
                add_source(f"names:{language}:{cluster_id}:story", clean(result.raw.get("cluster", {}).get("title")) or cluster_id,
                           {"story": clean(story["story"])})
        records = result.raw.get("cluster", {}).get("shortClusters", [])
        records = sorted((r for r in records if isinstance(r, dict)), key=lambda r: r.get("source") == "Photos")
        fetched = 0
        for record in records:
            if stopped:
                break
            number, label = record.get("id"), record.get("source")
            if not photo_id(number) or not isinstance(label, str) or not label or len(label) > 300:
                continue
            key = f"record:{number}:{label}"
            if key in result.raw or key in result.attempted_records:
                continue
            if cached and not (more or refresh):
                continue
            if fetched >= 2:
                break
            fetched += 1
            result.attempted_records.append(key)
            details = await fetch("GetSingleFullDetails", {"id": number, "lang": language, "source": label}, key)
            if details:
                if not isinstance(details.get("details"), list):
                    result.errors.append("GetSingleFullDetails: names_details_schema")
                    result.raw.pop(key, None)
                    continue
                # Indexed paths preserve multiple occurrences of the same field ID.
                fields = {f"details[{i}].{entry.get('id', '')}": clean(entry.get("value"))
                          for i, entry in enumerate(details["details"]) if isinstance(entry, dict) and clean(entry.get("value"))}
                name = clean(result.raw.get("cluster", {}).get("title")) or cluster_id
                add_source(f"names:{language}:{cluster_id}:record:{number}:{label}", f"{name} — {label}", fields)
        result.errors = list(dict.fromkeys(result.errors))
        return result
