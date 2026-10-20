"""Read one saved native answer without opening a voice session or querying CueKB."""

from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.evidence import EvidenceGate
from app.agent_runtime.native_cuekb import spoken_citations
from app.contracts import Citation, PresentationContract
from app.storage.models import Record, Turn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def inspect_answer(settings, conversation_id, input_item_id):
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            turns = list(await db.scalars(select(Turn).where(
                Turn.conversation_id == conversation_id,
                Turn.input_item_id == input_item_id,
            ).order_by(Turn.created_at.desc())))
            turn = next((t for t in turns if t.answer and t.evidence), None)
            if turn is None:
                raise ValueError("No committed answer with evidence exists for this conversation/input")
            records = list(await db.scalars(select(Record).where(
                Record.conversation_id == conversation_id, Record.epoch == turn.epoch,
                Record.kind == "voicechat_transcript",
            ).order_by(Record.created_at.desc())))
            records = [r for r in records if r.payload.get("input_item_id") == input_item_id]
            # Do not guess which of several answers was submitted to the observer.
            if len(records) != 1:
                raise ValueError(f"Expected one final transcript for this input; found {len(records)}")
            transcript = records[0]
            text = transcript.payload["text"]
            ready = EvidenceReady(turn.evidence["envelope"],
                                  [Citation.model_validate(c) for c in turn.evidence["citations"]], 0, "")
            contract = PresentationContract.model_validate(turn.answer["presentation"])
            delivered = spoken_citations(ready)
            failures = EvidenceGate.missing_conditions(text, delivered, contract.required_conditions)
            return {
                "conversation_id": conversation_id, "input_item_id": input_item_id,
                "response_id": transcript.source_id, "stored_reason": turn.answer.get("reason_code"),
                "original_check": EvidenceGate.check_presentation(text, contract, ready.citations),
                "delivered_check": EvidenceGate.check_presentation(text, contract, delivered),
                "condition_failures": failures[:3], "omitted_failures": max(0, len(failures) - 3),
                "voice_session_max_seconds": settings.voice_session_max_seconds,
                "answer_text": text[:8000], "answer_text_truncated": len(text) > 8000,
            }
    finally:
        await engine.dispose()
