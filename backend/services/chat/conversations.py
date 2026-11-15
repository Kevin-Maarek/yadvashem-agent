"""Server-owned chat memory; no transaction stays open during model calls."""
from uuid import uuid4

from sqlalchemy import select, update

from core.errors import StageError
from db.models import Conversation, ConversationTurn
from db.session import Session
from schemas.api import ChatResult


def create() -> dict:
    with Session.begin() as session:
        conversation = Conversation(id=str(uuid4()), revision=0, state={})
        session.add(conversation)
        session.flush()
        return {"id": conversation.id}


def load(conversation_id: str) -> dict:
    with Session() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise StageError("conversation_missing")
        query = select(ConversationTurn).where(ConversationTurn.conversation_id == conversation_id)
        query = query.order_by(ConversationTurn.sequence.desc()).limit(8)
        turns = list(reversed(session.scalars(query).all()))
        return {"id": conversation.id, "revision": conversation.revision, "state": conversation.state,
                "turns": [{"id": turn.id, "sequence": turn.sequence, "message": turn.message,
                           "result": turn.result} for turn in turns]}


def replay(conversation_id: str, request_id: str) -> ChatResult | None:
    with Session() as session:
        turn = session.scalar(select(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation_id, ConversationTurn.request_id == request_id))
        return ChatResult.model_validate(turn.result) if turn else None


def save(snapshot: dict, request_id: str, message: str, result: ChatResult, state: dict) -> ChatResult:
    with Session.begin() as session:
        changed = session.execute(update(Conversation).where(
            Conversation.id == snapshot["id"], Conversation.revision == snapshot["revision"]
        ).values(revision=snapshot["revision"] + 1, state=state)).rowcount
        if not changed:
            existing = replay(snapshot["id"], request_id)
            if existing:
                return existing
            raise StageError("conversation_conflict")
        session.add(ConversationTurn(conversation_id=snapshot["id"], request_id=request_id,
            sequence=snapshot["revision"] + 1, message=message, result=result.model_dump(mode="json")))
    return result
