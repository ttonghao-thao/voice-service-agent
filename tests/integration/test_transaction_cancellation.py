import asyncio

import pytest
from app.storage.models import Conversation
from app.storage.store import Store
from sqlalchemy import select


@pytest.mark.parametrize("rollback", [False, True])
async def test_cancel_waits_for_transaction_cleanup_before_unlocking(tmp_path, rollback):
    store = Store(f"sqlite+aiosqlite:///{tmp_path}/cancellation.db")
    await store.init_dev()
    exiting, complete, follower = asyncio.Event(), asyncio.Event(), asyncio.Event()
    native_begin = store.sessions.begin

    class DelayedExit:
        def __init__(self):
            self.context = native_begin()

        async def __aenter__(self):
            return await self.context.__aenter__()

        async def __aexit__(self, *exc):
            exiting.set()
            await complete.wait()
            return await self.context.__aexit__(*exc)

    store.sessions.begin = DelayedExit

    async def write():
        async with store.transaction() as db:
            db.add(Conversation(owner_id="synthetic-owner", title="canceled-exit"))
            await db.flush()
            if rollback:
                raise asyncio.CancelledError

    async def next_write():
        async with store.transaction() as db:
            follower.set()
            db.add(Conversation(owner_id="synthetic-owner", title="next-write"))

    first, second = None, None
    try:
        first = asyncio.create_task(write())
        await asyncio.wait_for(exiting.wait(), 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert store.write_lock.locked() and not first.done()
        second = asyncio.create_task(next_write())
        await asyncio.sleep(0)
        assert not follower.is_set()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 2)
        await asyncio.wait_for(second, 2)
        async with store.sessions() as db:
            titles = list((await db.execute(select(Conversation.title))).scalars())
        assert sorted(titles) == (["next-write"] if rollback else ["canceled-exit", "next-write"])
    finally:
        complete.set()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        await store.engine.dispose()
