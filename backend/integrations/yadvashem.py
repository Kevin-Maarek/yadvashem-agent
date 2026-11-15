import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

import httpx
from sqlalchemy.dialects.postgresql import insert

from core.config import settings
from core.errors import StageError
from core.hashing import fingerprint
from core.text import lexical, signature_key
from db.session import Session
from db.models import SourceGate

COLLECTIONS = "https://collections.yadvashem.org"
CDN = "https://assets.yadvashem.org"
CDN_PREFIX = "/image/upload/t_f_low_image/f_auto/q_auto/v1/remote_media/"
ASSET_PATH = re.compile(r"arch_srika/(?:[A-Za-z0-9_ -]{1,100}/){1,12}[A-Za-z0-9_ #-]{1,140}\.(?:jpe?g|png|webp|tiff?)", re.I)
PAGE_SIZE = 25
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/tiff"}
log = logging.getLogger(__name__)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str):
        self.parts.append(data)


def clean(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = PlainText()
    parser.feed(value.replace("\x00", ""))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def values(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    names = []
    for item in items:
        if isinstance(item, dict):
            names.extend(values(item.get("value") or item.get("name") or item.get("title")))
        else:
            name = clean(item)
            if name:
                names.append(name)
    return list(dict.fromkeys(names))


def photo_id(value) -> int | None:
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value)):
        number = int(value)
        if 0 < number < 2**63:
            return number
    return None


def resolve_image(reference: str, source: str) -> tuple[str, str] | None:
    if not isinstance(reference, str) or not reference or any(ord(ch) < 32 for ch in reference):
        return None
    raw = reference.replace("\\", "/")
    query = ""
    if raw.startswith("https://"):
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return None
        if parsed.netloc != "assets.yadvashem.org" or not parsed.path.startswith(CDN_PREFIX) or parsed.fragment:
            return None
        query = parsed.query
        raw = unquote(parsed.path.removeprefix(CDN_PREFIX))
    else:
        # In a source file reference, '#' is a literal filename character, not a URL fragment.
        raw, _, query = raw.partition("?")
        if raw.startswith("//arch_srika/"):
            raw = raw[2:]
        elif raw.startswith("/arch_srika/"):
            raw = raw[1:]
    if query and not re.fullmatch(r"_a=[A-Za-z0-9_-]+", query):
        return None
    # The supplied 99327 response includes an extra separator directly after this root.
    # Interior empty segments and dot segments remain invalid.
    raw = re.sub(r"^arch_srika/+", "arch_srika/", raw)
    if not ASSET_PATH.fullmatch(raw):
        return None
    url = CDN + CDN_PREFIX + quote(raw, safe="/")
    return raw, url + ("?" + query if query else "")


def image_reference(item: dict) -> str:
    for key in ("url", "image", "imageUrl", "path", "filePath", "imagePath", "fileName", "src", "thmbnl"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def normalized_images(items, source: str) -> list[dict]:
    result = {}
    for position, item in enumerate(items):
        data = item if isinstance(item, dict) else {"image": item}
        front = image_reference(data)
        if not front:
            continue
        back = data.get("backImage")
        if isinstance(back, dict):
            back = image_reference(back)
        back = back.strip() if isinstance(back, str) and back.strip() else None
        front_resolved = resolve_image(front, source)
        back_resolved = resolve_image(back, source) if back else None
        canonical_front = front_resolved[0] if front_resolved else front
        canonical_back = back_resolved[0] if back_resolved else back
        identity = fingerprint(canonical_front)
        image = {
            "identity": identity, "position": position,
            "front_reference": front, "back_reference": back,
            "front_url": front_resolved[1] if front_resolved else None,
            "back_url": back_resolved[1] if back_resolved else None,
            "reference_fingerprint": fingerprint(canonical_front, canonical_back),
        }
        if identity not in result or (back and not result[identity]["back_reference"]):
            result[identity] = image
    return list(result.values())


def source_back_reference(reference: str, detail_images: list[dict], source: str) -> str:
    """Resolve an explicit back link to a unique asset actually listed in Full Details."""
    resolved = resolve_image(reference, source)
    if resolved is None:
        return reference

    def match_key(path):
        directory, _, filename = path.rpartition("/")
        stem, _, extension = filename.rpartition(".")
        # Search sometimes omits directory spaces or changes extension case.
        # Keep the filename stem exact and never invent an unlisted asset URL.
        return directory.replace(" ", ""), stem, extension.lower()

    candidates = {}
    for image in detail_images:
        candidate = resolve_image(image["front_reference"], source)
        if candidate:
            if candidate[0] == resolved[0]:
                return reference
            if match_key(candidate[0]) == match_key(resolved[0]):
                candidates[candidate[0]] = image["front_reference"]
    return next(iter(candidates.values())) if len(candidates) == 1 else reference


def resource_entries(details: dict):
    resources = details.get("resources")
    if isinstance(resources, list):
        for index, resource in enumerate(resources):
            if not isinstance(resource, dict):
                continue
            prefix = f"resources[{index}]"
            if resource.get("type") == "details" and isinstance(resource.get("elements"), list):
                for position, element in enumerate(resource["elements"]):
                    if isinstance(element, dict) and isinstance(element.get("title"), str):
                        yield element["title"], element.get("value"), f"{prefix}.elements[{position}].value"


def collect_fields(entries) -> tuple[dict[str, list[str]], dict[str, str]]:
    fields, paths = {}, {}
    for key, value, path in entries:
        parsed = values(value)
        if parsed:
            fields[key] = list(dict.fromkeys([*fields.get(key, []), *parsed]))
            paths[key] = path
    return fields, paths


def resource_fields(details: dict) -> dict:
    return collect_fields(resource_entries(details))[0]


def normalize(source: str, id_number: int, card: dict | None, details: dict) -> tuple[dict, list[dict]]:
    card = card or {}
    resources, resource_paths = collect_fields(resource_entries(details))
    related = card.get("relatedList")
    related = related if isinstance(related, list) else []
    search_fields, search_paths = collect_fields(
        (entry["id"], entry.get("value"), f"relatedList[{index}].value")
        for index, entry in enumerate(related)
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )
    provenance = {}

    def pick(name: str, *candidates, transform=clean):
        for origin, field, value in candidates:
            normalized = transform(value)
            if normalized:
                provenance[name] = {"source": origin, "field": field}
                return normalized
        return None

    title = pick("title", ("details", "title", details.get("title")), ("search", "title", card.get("title"))) or ""
    description = pick("catalog_description", ("details", "infoText", details.get("infoText")),
                       ("search", "text", card.get("text"))) or ""
    subjects = pick("subjects", ("details", "subjects", details.get("subjects")),
                    ("details", resource_paths.get("subjects", ""), resources.get("subjects")),
                    ("search", "subjects", card.get("subjects")), transform=values) or []
    for origin, path, candidates in (
        ("details", resource_paths.get("subjects", ""), resources.get("subjects")),
        ("search", "subjects", card.get("subjects")),
    ):
        added = [subject for subject in values(candidates) if subject not in subjects]
        if added:
            subjects.extend(added)
            provenance["subjects"].setdefault("contributors", []).append({"source": origin, "field": path})
    collection_title = pick("relatedTitle", ("details", "relatedTitle", details.get("relatedTitle")),
                            ("details", resource_paths.get("relatedCollection", ""), resources.get("relatedCollection")),
                            ("search", search_paths.get("relatedCollection", ""), search_fields.get("relatedCollection")),
                            ("search", "relatedTitle", card.get("relatedTitle")),
                            transform=lambda raw: ", ".join(values(raw))) or ""
    metadata = {}
    # Preserve new catalog keys as they appear, alongside the untouched raw responses.
    for origin, fields, paths in (("search", search_fields, search_paths), ("details", resources, resource_paths)):
        for key, entries in fields.items():
            metadata[key] = entries if len(entries) > 1 else entries[0]
            if key not in ("subjects", "relatedTitle", "title", "catalog_description"):
                provenance[key] = {"source": origin, "field": paths[key]}
    if subjects:
        metadata["subjects"] = subjects
    for key in ("signature", "submitter", "source", "credit", "date", "materialType"):
        search_key = "archivalSignature" if key == "signature" else key
        value = pick(key, ("details", resource_paths.get(key, ""), resources.get(key)),
                     ("details", key, details.get(key)),
                     ("search", search_paths.get(search_key, ""), search_fields.get(search_key)),
                     ("search", key, card.get(key)), transform=values)
        if value:
            metadata[key] = value if key == "materialType" else ", ".join(value)
    for key in ("relatedId", "collectionId", "albumId", "photoSource"):
        if details.get(key) not in (None, "", [], {}):
            metadata[key] = details[key]
            provenance[key] = {"source": "details", "field": key}
    if collection_title:
        metadata["relatedTitle"] = collection_title
    for index, resource in enumerate(details.get("resources") or []):
        if isinstance(resource, dict) and resource.get("type") == "map" and isinstance(resource.get("elements"), dict):
            metadata.setdefault("map", []).append(resource["elements"])
            provenance.setdefault("map", {"source": "details", "fields": []})["fields"].append(
                f"resources[{index}].elements")
    collection_key = next((f"{key}:{details[key]}" for key in ("collectionId", "albumId")
                           if isinstance(details.get(key), (str, int)) and details[key]), None)
    catalog_text = "\n".join(f"{label}: {value}" for label, value in (
        ("כותרת", title), ("תיאור קטלוגי", description), ("נושאים", ", ".join(subjects)),
        ("אוסף קשור", collection_title),
    ) if value)
    catalog_lexical = " ".join([title, description, *subjects, collection_title,
                               *(metadata.get(key, "") for key in ("signature", "source", "credit", "date"))])
    source_url = f"{COLLECTIONS}/he/photos/{id_number}"
    if source_url:
        provenance["source_url"] = {"source": "collections", "field": "photo_route"}
    image_data = details.get("images")
    detail_images = normalized_images(image_data if isinstance(image_data, list) else [], source)
    search_images = card.get("images")
    search_images = normalized_images(search_images if isinstance(search_images, list) else [], source)
    by_identity = {image["identity"]: image for image in detail_images}
    for image in search_images:
        existing = by_identity.get(image["identity"])
        if existing is None:
            image["position"] = len(by_identity)
            by_identity[image["identity"]] = image
        elif not existing["back_reference"] and image["back_reference"]:
            for key in ("back_reference", "back_url", "reference_fingerprint"):
                existing[key] = image[key]
    # Full Details can list a reverse as a standalone asset. Only an explicit
    # backImage association from the source establishes that it is the same item.
    reverse_identities = set()
    for image in by_identity.values():
        if image["back_reference"]:
            corrected = source_back_reference(image["back_reference"], detail_images, source)
            if corrected != image["back_reference"]:
                paired = normalized_images([{"url": image["front_reference"], "backImage": corrected}], source)[0]
                for key in ("back_reference", "back_url", "reference_fingerprint"):
                    image[key] = paired[key]
            resolved = resolve_image(image["back_reference"], source)
            reverse_identities.add(fingerprint(resolved[0] if resolved else image["back_reference"]))
    logical_images = [image for identity, image in by_identity.items()
                      if identity not in reverse_identities or image["back_reference"]]
    return {
        "source_url": source_url, "title": title or f"פריט {id_number}", "title_norm": lexical(title),
        "catalog_description": description, "subjects": subjects,
        "signature_norm": signature_key(metadata.get("signature", "")),
        "names_norm": [], "places_norm": [],
        "year_start": None, "year_end": None, "collection_key": collection_key,
        "metadata_fields": metadata, "provenance": provenance, "raw_search": card or None,
        "raw_details": details, "catalog_text": catalog_text, "catalog_fingerprint": fingerprint(catalog_text),
        "lexical": lexical(catalog_lexical),
    }, logical_images


def retry_after(value: str | None, now: datetime) -> datetime:
    if value:
        try:
            return now + timedelta(seconds=max(1, int(value)))
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                return max(date, now + timedelta(seconds=1))
            except (ValueError, OverflowError, TypeError):
                pass
    return now + timedelta(seconds=30)


def reserve_source_request():
    while True:
        now = datetime.now(UTC)
        with Session.begin() as session:
            session.execute(insert(SourceGate).values(id=1, blocked=False).on_conflict_do_nothing())
            gate = session.get(SourceGate, 1, with_for_update=True)
            if gate.blocked:
                raise StageError("source_forbidden")
            if gate.next_request_at is None or gate.next_request_at <= now:
                gate.next_request_at = now + timedelta(seconds=settings.source_delay_seconds)
                return
            delay = (gate.next_request_at - now).total_seconds()
        if delay > settings.source_delay_seconds + 0.5:
            raise StageError("source_cooldown", retry=True, wait_until=now + timedelta(seconds=delay))
        time.sleep(max(0, delay))


async def bounded_request(client: httpx.AsyncClient, method: str, url: str, *,
                          asset=False, expected_count=False, record_lookup=False, expected_empty=False, **kwargs):
    await asyncio.to_thread(reserve_source_request)
    started = time.monotonic()
    status = 0
    limit = MAX_IMAGE_BYTES if asset else 2 * 1024 * 1024
    headers = {"Accept": "image/jpeg,image/png,image/webp,image/tiff" if asset else "application/json",
               **kwargs.pop("headers", {})}
    try:
        async with client.stream(method, url, headers=headers, follow_redirects=False, **kwargs) as response:
            status = response.status_code
            if status in (403, 429):
                now = datetime.now(UTC)
                deadline = retry_after(response.headers.get("Retry-After"), now) if status == 429 else None
                with Session.begin() as session:
                    gate = session.get(SourceGate, 1, with_for_update=True)
                    gate.blocked = status == 403
                    if deadline and (gate.next_request_at is None or deadline > gate.next_request_at):
                        gate.next_request_at = deadline
                raise StageError("source_forbidden" if status == 403 else "source_rate_limited",
                                 retry=status == 429, wait_until=deadline)
            if status == 404 or (record_lookup and status == 204):
                code = "image_missing" if asset else "record_missing" if record_lookup else "source_http_404"
                raise StageError(code)
            if status != 200:
                raise StageError(f"source_http_{status}", retry=status == 408 or status >= 500)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if (asset and content_type not in ALLOWED_IMAGE_TYPES) or (
                    not asset and not expected_empty and content_type != "application/json"):
                raise StageError("image_type" if asset else "source_content_type")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise StageError("image_too_large" if asset else "source_too_large")
            data = bytearray()
            async for block in response.aiter_bytes(chunk_size=65536):
                if len(data) + len(block) > limit:
                    raise StageError("image_too_large" if asset else "source_too_large")
                data.extend(block)
            if asset:
                return bytes(data), content_type
            if expected_empty:
                if bytes(data).strip() not in (b"", b"null"):
                    raise StageError("source_empty_schema")
                return None
            try:
                decoded = json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise StageError("source_json", retry=True) from exc
            if record_lookup and decoded in (None, {}, []):
                raise StageError("record_missing")
            if expected_count:
                if type(decoded) is not int or decoded < 0:
                    raise StageError("source_count_schema")
            elif not isinstance(decoded, dict):
                raise StageError("source_schema")
            return decoded
    except httpx.RequestError as exc:
        raise StageError("source_network", retry=True) from exc
    finally:
        log.info("source_request", extra={"metrics": {"status": status, "asset": asset,
                                                       "ms": round((time.monotonic() - started) * 1000)}})


class Source:
    def __init__(self, client: httpx.AsyncClient, kind: str, *, client_id: str | None = None):
        if kind != "live":
            raise StageError("source_kind")
        self.client = client
        self.kind = kind
        # The public Search API uses this as query context even without authentication.
        self.search_headers = {"x-client-id": client_id or str(uuid.uuid4())}

    @staticmethod
    def filters() -> list[dict]:
        return [
            {"fieldName": "data_bank", "filterType": 0, "filterOperator": 0, "values": ["photos"]},
            {"fieldName": "languages_page", "filterType": 0, "filterOperator": 0, "values": ["he"]},
        ]

    async def count(self) -> int:
        return await bounded_request(
            self.client, "POST", f"{COLLECTIONS}/api/Search/GetCountOfDocuments",
            params={"valueToSearch": "", "queryOperator": 0},
            json={"filters": self.filters()}, headers=self.search_headers, expected_count=True,
        )

    async def page(self, page: int) -> tuple[list[dict], int | None]:
        body = {
            "queryOperator": 0,
            "filters": {"logicOperator": 0, "filters": self.filters()},
            "currentTab": {"id": "tab_type", "value": ""},
        }
        response = await bounded_request(self.client, "POST", f"{COLLECTIONS}/api/Search/GetDataResultsQuery",
                                         params={"valueToSearch": "", "pageNumber": page, "pageSize": PAGE_SIZE,
                                                 "lang": "he", "cardType": "card"},
                                         json=body, headers=self.search_headers)
        cards, count = response.get("cards"), response.get("count")
        if not isinstance(cards, list) or len(cards) > PAGE_SIZE:
            raise StageError("source_cards_schema")
        if any(not isinstance(card, dict) or photo_id(card.get("id")) is None for card in cards):
            raise StageError("source_card_id")
        return cards, count if type(count) is int and count >= 0 else None

    async def details(self, number: int) -> dict:
        details = await bounded_request(self.client, "GET", f"{COLLECTIONS}/api/Photos/GetFullDetails",
                                        params={"lang": "he", "id": number}, record_lookup=True)
        if not any(details.get(key) for key in ("title", "infoText", "images", "resources")):
            raise StageError("record_missing")
        return details

    async def image(self, reference: str) -> tuple[bytes, str]:
        resolved = resolve_image(reference, self.kind)
        if resolved is None:
            raise StageError("image_reference")
        _, url = resolved
        return await bounded_request(self.client, "GET", url, asset=True)
