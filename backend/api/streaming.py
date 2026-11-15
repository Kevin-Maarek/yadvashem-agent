import json

from fastapi.encoders import jsonable_encoder

SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def pack_event(name: str, value, event_id: int | None = None) -> str:
    identifier = f"id: {event_id}\n" if event_id is not None else ""
    return f"{identifier}event: {name}\ndata: {json.dumps(jsonable_encoder(value), ensure_ascii=False)}\n\n"

