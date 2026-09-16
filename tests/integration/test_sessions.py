import asyncio

import pytest
from app.api.routes import _answer_for_principal, _event_for_principal
from app.contracts import AnswerBundle, DomainError, Principal
from app.storage.models import Turn
from sqlalchemy import select


def dev_user():
    return Principal(
        user_id="dev-operator",
        tenant_id="dev-tenant",
        scopes=frozenset({"knowledge:read", "weather:read"}),
        knowledge_base_ids=("kb_support",),
    )


async def test_chinese_text_and_idempotency(client, app, conversation):
    headers = {"Idempotency-Key": "same"}
    first = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        headers=headers,
        json={"text": "张先生，请查询联调示例：产品型号 AX-中文。"},
    )
    assert first.status_code == 202
    task = app.state.coordinator.tasks.get(conversation)
    if task:
        await task
    duplicate = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        headers=headers,
        json={"text": "张先生，请查询联调示例：产品型号 AX-中文。"},
    )
    assert duplicate.json()["turn_id"] == first.json()["turn_id"]
    data = (await client.get(f"/api/v1/conversations/{conversation}/messages")).json()
    assert len(data["items"]) == 1
    answer = data["items"][0]["answer"]
    assert answer["is_mock"] and answer["citations"][0]["citation_id"] == "C1"
    assert "合成" in answer["display_text"]
    changed = await client.post(
        f"/api/v1/conversations/{conversation}/messages", headers=headers, json={"text": "另一问题"}
    )
    assert changed.status_code == 409


async def test_missing_idempotency_and_extra_identity_rejected(client, conversation):
    path = f"/api/v1/conversations/{conversation}/messages"
    assert (await client.post(path, json={"text": "你好"})).status_code == 422
    assert (
        await client.post(path, headers={"Idempotency-Key": "x"}, json={"text": "你好", "tenant_id": "other"})
    ).status_code == 422


async def test_empty_rag_never_fabricates(client, app, conversation):
    response = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        headers={"Idempotency-Key": "empty"},
        json={"text": "退款政策是什么"},
    )
    assert response.status_code == 202
    task = app.state.coordinator.tasks.get(conversation)
    if task:
        await task
    data = (await client.get(f"/api/v1/conversations/{conversation}/messages")).json()
    assert data["items"][0]["answer"]["status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "other",
    [
        Principal(user_id="other", tenant_id="dev-tenant"),
        Principal(user_id="dev-operator", tenant_id="other"),
    ],
)
async def test_object_access_isolation(app, conversation, other):
    async with app.state.store.sessions() as db:
        with pytest.raises(DomainError):
            await app.state.store.get(db, conversation, other)


async def test_cancel_resistant_result_cannot_commit(app, conversation):
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return AnswerBundle(status="answered", display_text="旧城市数据", speech_text="旧城市数据")

    app.state.coordinator.runtime.run = stubborn
    t, task = await app.state.coordinator.submit(dev_user(), conversation, "race", "旧城市")
    await entered.wait()
    new_epoch = await app.state.coordinator.interrupt(dev_user(), conversation, t.epoch)
    assert new_epoch == t.epoch + 1
    release.set()
    assert await task is None
    async with app.state.store.sessions() as db:
        saved = await db.get(Turn, t.id)
        c = await app.state.store.get(db, conversation)
        assert saved.status == "canceled" and saved.answer is None and c.history == []


async def test_replayed_interrupt_does_not_cancel_new_turn(app, conversation):
    user = dev_user()
    first = await app.state.coordinator.interrupt(user, conversation, 0)
    t, task = await app.state.coordinator.submit(user, conversation, "voice:1:x", "联调示例", "voice", first)
    assert await app.state.coordinator.interrupt(user, conversation, 0) == first
    assert await task is not None
    async with app.state.store.sessions() as db:
        assert (await db.get(Turn, t.id)).status == "answered"


async def test_new_text_cancels_old_and_preserves_only_committed_history(app, conversation):
    original = app.state.coordinator.runtime.run
    entered = asyncio.Event()

    async def controlled(request, *args):
        if request == "slow":
            entered.set()
            await asyncio.Event().wait()
        return await original(request, *args)

    app.state.coordinator.runtime.run = controlled
    old, old_task = await app.state.coordinator.submit(dev_user(), conversation, "slow", "slow")
    await entered.wait()
    new, new_task = await app.state.coordinator.submit(dev_user(), conversation, "new", "联调示例")
    await new_task
    await asyncio.gather(old_task, return_exceptions=True)
    async with app.state.store.sessions() as db:
        c = await app.state.store.get(db, conversation)
        assert len(c.history) == 2 and c.history[0]["content"] == "联调示例"
        assert (await db.get(Turn, old.id)).status == "canceled"
        assert new.epoch > old.epoch


async def test_tool_revocation_and_admin_permissions(client, app, conversation):
    assert (
        await client.patch("/api/v1/admin/tools/search_knowledge", json={"enabled": False})
    ).status_code == 200
    _, task = await app.state.coordinator.submit(dev_user(), conversation, "disabled", "联调示例")
    answer = await task
    assert not answer.citations
    app.state.settings.dev_admin = False
    assert (await client.get("/api/v1/admin/tools")).status_code == 403


async def test_customer_isolation_admin_denial_and_kb_revocation(client, app):
    app.state.settings.knowledge_base_ids = "kb_support,kb_private"
    identity = {
        "principal": Principal(
            user_id="customer-a",
            tenant_id="dev-tenant",
            roles=frozenset({"customer"}),
            scopes=frozenset({"knowledge:read"}),
            knowledge_base_ids=("kb_private", "kb_support"),
        )
    }

    async def current_principal(_request):
        return identity["principal"]

    app.state.auth.principal = current_principal
    created = await client.post("/api/v1/conversations", json={})
    assert created.status_code == 201
    cid = created.json()["id"]
    sent = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        headers={"Idempotency-Key": "customer-answer"},
        json={"text": "联调示例"},
    )
    assert sent.status_code == 202
    await app.state.coordinator.tasks[cid]
    assert (await client.get("/api/v1/admin/tools")).status_code == 403
    before = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()
    assert before["items"][0]["answer"]["citations"]
    legacy_answer = dict(before["items"][0]["answer"])
    legacy_answer["citations"] = [dict(legacy_answer["citations"][0])]
    legacy_answer["citations"][0].pop("authorized_kb_ids")
    assert (
        _answer_for_principal(legacy_answer, identity["principal"], app.state.settings)[
            "reason_code"
        ]
        == "KB_ACCESS_REVOKED"
    )
    async with app.state.store.sessions() as db:
        current_epoch = (await app.state.store.get(db, cid)).epoch
    await app.state.store.record(
        cid,
        current_epoch,
        "voicechat_transcript",
        "spoken-private",
        {
            "response_id": "spoken-private",
            "text": "旧授权范围的口述内容",
            "_authorized_kb_ids": ["kb_private", "kb_support"],
        },
    )

    identity["principal"] = identity["principal"].model_copy(
        update={"knowledge_base_ids": ("kb_support",)}
    )
    after = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()
    assert after["items"][0]["answer"]["reason_code"] == "KB_ACCESS_REVOKED"
    assert after["items"][0]["answer"]["citations"] == []
    assert not any(record["source_id"] == "spoken-private" for record in after["records"])
    async with app.state.store.sessions() as db:
        stored_history = (await app.state.store.get(db, cid)).history
    visible_history = app.state.coordinator.authorized_history(
        stored_history, identity["principal"]
    )
    assert not any(item["role"] == "assistant" for item in visible_history)

    voice = await client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={})
    assert voice.status_code == 201
    session = app.state.voice.sessions[voice.json()["voice_session_id"]]
    assert "合成联调资料" not in session.summary

    async with app.state.store.sessions() as db:
        from app.storage.models import Event

        events = (
            await db.execute(select(Event).where(Event.conversation_id == cid))
        ).scalars()
        event = next(row for row in events if row.payload["type"] == "portal.answer.final")
    safe = _event_for_principal(event.payload, identity["principal"], app.state.settings)
    assert safe["payload"]["reason_code"] == "KB_ACCESS_REVOKED"

    identity["principal"] = identity["principal"].model_copy(update={"user_id": "customer-b"})
    assert (await client.get(f"/api/v1/conversations/{cid}/messages")).status_code == 404


async def test_origin_and_limits(client, app):
    assert (
        await client.post("/api/v1/conversations", headers={"Origin": "https://evil.invalid"}, json={})
    ).status_code == 403
    app.state.settings.request_limit_per_minute = 1
    assert (await client.post("/api/v1/conversations", json={})).status_code == 201
    assert (await client.post("/api/v1/conversations", json={})).status_code == 429


async def test_voice_ticket_capacity_expiry_and_text_switch(client, app, conversation):
    app.state.settings.max_voice_sessions = 1
    first = await client.post(f"/api/v1/conversations/{conversation}/voice-sessions", json={})
    assert first.status_code == 201
    sid = first.json()["voice_session_id"]
    assert (
        await client.post(f"/api/v1/conversations/{conversation}/voice-sessions", json={})
    ).status_code == 429
    app.state.voice.sessions[sid].expires = 0
    second = await client.post(f"/api/v1/conversations/{conversation}/voice-sessions", json={})
    assert second.status_code == 201
    _, task = await app.state.coordinator.submit(dev_user(), conversation, "switch", "联调示例")
    await task
    assert not app.state.voice.sessions


async def test_startup_recovery_invalidates_active_state(app, conversation):
    async with app.state.store.transaction() as db:
        c = await app.state.store.get(db, conversation, lock=True)
        c.voice_session_id = "old"
    await app.state.coordinator.recover()
    async with app.state.store.sessions() as db:
        c = await app.state.store.get(db, conversation)
        assert c.epoch == 1 and c.voice_session_id is None


async def test_transcript_done_deduplication_and_playback_progress(app, conversation):
    store = app.state.store
    assert await store.record(conversation, 0, "user_transcript", "item-1", {"text": "中文"})
    assert not await store.record(conversation, 0, "user_transcript", "item-1", {"text": "中文"})
    assert not await store.record(conversation, 4, "user_transcript", "item-1", {"text": "旧结果"})
    assert await store.record(conversation, 0, "playback_ack", "response-1", {"played_samples": 100})
    await store.record(conversation, 0, "playback_ack", "response-1", {"played_samples": 50})
    from app.storage.models import Record

    async with store.sessions() as db:
        row = (await db.execute(select(Record).where(Record.kind == "playback_ack"))).scalar_one()
        assert row.payload["played_samples"] == 100


async def test_total_deadline_also_covers_runtime_preparation(app, conversation):
    app.state.settings.agent_deadline_ms = 100

    async def delayed_runtime(*args):
        await asyncio.sleep(1)
        raise AssertionError("Deadline must cancel before any success result")

    app.state.coordinator.runtime.run = delayed_runtime
    _, task = await app.state.coordinator.submit(dev_user(), conversation, "deadline", "联调示例")
    result = await task
    assert result.status == "failed" and result.reason_code == "AGENT_TIMEOUT"
