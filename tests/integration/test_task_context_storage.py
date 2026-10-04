import pytest
from app.agent_runtime.context import RunContext
from app.contracts import AnswerBundle, DomainError, Principal
from app.storage.models import Turn

SUPPORT = "00000000-0000-4000-8000-000000000001"
PRIVATE = "00000000-0000-4000-8000-000000000002"


def user():
    return Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(SUPPORT,))


async def test_context_combines_general_dialogue_with_current_authorized_history(app, conversation):
    store = app.state.store
    await store.utterance(user(), conversation, 0, "general-input", text="My model is AX100, version 2.1.",
        answer=AnswerBundle(status="answered", display_text="Unverified background", speech_text=""))
    async with store.transaction() as db:
        c = await store.get(db, conversation)
        c.history = [{"role": "user", "content": "Old user request"},
                     {"role": "assistant", "content": "Revoked company fact", "authorized_kb_ids": [PRIVATE]}]
    captured = []

    async def runtime(request, ctx, history, progress):
        captured.append((ctx, history))
        return AnswerBundle(status="answered", display_text="Authorized result", speech_text="Authorized result")

    app.state.coordinator.runtime.run = runtime
    turn, task = await app.state.coordinator.submit(user(), conversation, "context", "What is its limit?")
    await task
    ctx, history = captured[0]
    assert not any("Revoked company fact" in item["content"] for item in history)
    assert any("Unverified general reply; not knowledge evidence" in item["content"] for item in history)
    assert ctx.slots == {"product_model": "AX100", "software_version": "2.1"}
    async with store.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.task_context["authorized_kb_ids"] == [SUPPORT]
        assert saved.task_context["conditions"]["product_model"]["source"]["input_item_id"] == "general-input"
        assert saved.task_context["deadline_at"] and saved.task_context["turn_id"] == turn.id


async def test_final_asr_is_immutable_and_an_old_epoch_cannot_change_conditions(app, conversation):
    store = app.state.store
    await store.utterance(user(), conversation, 0, "input", text="My model is AX100.")
    with pytest.raises(DomainError) as exc:
        await store.utterance(user(), conversation, 0, "input", text="My model is AX200.")
    assert exc.value.code == "VOICE_PROTOCOL_ERROR"
    await store.rotate_voice(user(), conversation)
    assert not await store.utterance(user(), conversation, 0, "late", text="My model is AX300.")
    async with store.sessions() as db:
        c = await store.get(db, conversation)
        assert c.slots == {"product_model": "AX100"}


async def test_legacy_unsourced_slots_do_not_become_confirmed_context(app, conversation):
    store = app.state.store
    async with store.transaction() as db:
        c = await store.get(db, conversation)
        c.slots = {"product_model": "Unproven model", "software_version": "Unproven version"}
    turn, c, _ = await store.begin_turn(user(), conversation, "legacy", "Find the integration sample", "text")
    assert not turn.task_context["conditions"] and c.slots == {}


@pytest.mark.parametrize("bad_filter", [123, {"untrusted": "AX100"}])
async def test_sdk_filter_schema_is_checked_before_context_binding(app, conversation, bad_filter):
    turn, _, _ = await app.state.store.begin_turn(user(), conversation, "bad-sdk", "Find the integration sample", "voice")
    ctx = RunContext(user(), conversation, turn.id, turn.epoch, request_revision=turn.request_revision,
                     task_context=turn.task_context)
    await app.state.coordinator.runtime.prepare(ctx)
    result = await app.state.registry.invoke("search_knowledge", {
        "query": "Find the integration sample", "product_model": bad_filter}, ctx)
    assert result["code"] == "TOOL_BAD_RESPONSE" and ctx.evidence == {} and ctx.retrieval_calls == 0
