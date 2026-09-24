import httpx
import pytest
from app.api.auth import Auth
from app.config import Settings
from app.main import create_app

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


async def test_validation_identity_is_server_owned_and_customer_only():
    settings = Settings(
        _env_file=None,
        auth_mode="validation",
        tenant_id="tenant-a",
        knowledge_base_ids=KB_SUPPORT,
    )
    principal = await Auth(settings).principal(None)

    assert principal.user_id == "validation-customer"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == {"customer"}
    assert principal.scopes == {"knowledge:read"}
    assert principal.knowledge_base_ids == (KB_SUPPORT,)


async def test_fixture_identity_retains_admin_support_for_injected_tests():
    settings = Settings(_env_file=None, dev_admin=True, knowledge_base_ids=KB_SUPPORT)
    principal = await Auth(settings).principal(None)

    assert principal.user_id == "dev-operator"
    assert {"operator", "admin"} <= principal.roles
    assert "tools:admin" in principal.scopes


async def test_validation_portal_needs_no_login_and_cannot_use_admin_api(tmp_path):
    settings = Settings(
        _env_file=None,
        auth_mode="validation",
        tenant_id="tenant-a",
        knowledge_base_ids=KB_SUPPORT,
        auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/validation.db",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://portal.test"
        ) as client:
            me = await client.get("/api/v1/auth/me")
            assert me.status_code == 200
            assert me.json()["user_id"] == "validation-customer"
            assert me.json()["auth_mode"] == "validation"

            conversation = await client.post("/api/v1/conversations", json={"title": "Validation"})
            assert conversation.status_code == 201
            forbidden = await client.get("/api/v1/admin/tools")
            assert forbidden.status_code == 403


async def test_normal_api_startup_rejects_fixture_configuration(monkeypatch):
    monkeypatch.setattr("app.main.Settings", lambda: Settings(_env_file=None))
    app = create_app()
    with pytest.raises(ValueError, match="fixture identity"):
        async with app.router.lifespan_context(app):
            pass


async def test_chunked_body_limit_and_drain(client, app, conversation):
    async def chunks():
        yield b"x" * 10000
        yield b"x" * 10000

    response = await client.post("/api/v1/conversations", content=chunks())
    assert response.status_code == 413
    assert (await client.post("/api/v1/admin/drain", json={})).status_code == 200
    response = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"text": "hello"},
        headers={"Idempotency-Key": "drain"},
    )
    assert response.status_code == 503
    assert (await client.get("/health/ready")).status_code == 503
