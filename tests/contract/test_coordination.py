import pytest
from app.contracts import DomainError
from app.sessions.coordination import Coordination


class RedisContract:
    """Explicit in-memory Redis command fixture; does not certify real Redis failover."""

    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value.encode()
        return True

    async def get(self, key):
        return self.data.get(key)

    async def eval(self, script, count, key, token):
        if self.data.get(key) != token.encode():
            return 0
        if "'del'" in script:
            self.data.pop(key, None)
        return 1


async def test_lease_exclusion_expiry_and_old_owner_release(app):
    first, second = Coordination(app.state.settings), Coordination(app.state.settings)
    backend = RedisContract()
    first.redis = second.redis = backend
    assert await first.acquire("conversation")
    with pytest.raises(DomainError, match="SESSION_OWNED_BY_OTHER"):
        await second.acquire("conversation")
    backend.data.clear()  # Explicit lease expiration fixture.
    assert await second.acquire("conversation")
    await first.release("conversation")
    await second.check("conversation")
    with pytest.raises(DomainError, match="GATEWAY_LEASE_LOST"):
        await first.check("conversation")


async def test_new_owner_advances_durable_epoch_before_new_output(app, conversation):
    from app.contracts import Principal
    from app.sessions.coordinator import SessionCoordinator

    user = Principal(user_id="dev-operator", tenant_id="dev-tenant")
    backend = RedisContract()
    one, two = Coordination(app.state.settings), Coordination(app.state.settings)
    one.redis = two.redis = backend
    a = SessionCoordinator(app.state.store, app.state.coordinator.runtime, one, app.state.settings)
    b = SessionCoordinator(app.state.store, app.state.coordinator.runtime, two, app.state.settings)
    async with a.lock(conversation):
        await a.ensure_owner(user, conversation)
    assert await app.state.store.current(conversation, 1)
    backend.data.clear()
    async with b.lock(conversation):
        await b.ensure_owner(user, conversation)
    assert not await app.state.store.current(conversation, 1)
    assert await app.state.store.current(conversation, 2)
