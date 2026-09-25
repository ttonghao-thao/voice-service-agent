import httpx
import pytest
from app.api.auth import Auth
from app.config import Settings
from app.main import create_app

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


async def test_fixture_identity_retains_admin_support_for_injected_tests():
    settings = Settings(_env_file=None, dev_admin=True, knowledge_base_ids=KB_SUPPORT)
    principal = Auth(settings, object()).fixture_principal()

    assert principal.user_id == "dev-operator"
    assert {"operator", "admin"} <= principal.roles
    assert "tools:admin" in principal.scopes


async def test_validation_portal_issues_isolated_per_call_access_and_denies_admin(tmp_path):
    settings = Settings(
        _env_file=None,
        auth_mode="validation",
        knowledge_base_ids=KB_SUPPORT,
        auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/validation.db",
        request_limit_per_minute=1,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://portal.test"
        ) as client:
            first = await client.post(
                "/api/v1/conversations",
                headers={"X-Forwarded-For": "192.0.2.1"},
                json={"title": "First"},
            )
            second = await client.post(
                "/api/v1/conversations",
                headers={"X-Forwarded-For": "192.0.2.2"},
                json={"title": "Second"},
            )
            assert first.status_code == second.status_code == 201
            assert (
                await client.post(
                    "/api/v1/conversations",
                    headers={"X-Forwarded-For": "192.0.2.1"},
                    json={"title": "Rate limited"},
                )
            ).status_code == 429
            assert first.json()["access_token"] != second.json()["access_token"]
            first_headers = {"Authorization": "Bearer " + first.json()["access_token"]}
            second_headers = {"Authorization": "Bearer " + second.json()["access_token"]}
            first_path = f"/api/v1/conversations/{first.json()['id']}/messages"
            assert (await client.get(first_path, headers=first_headers)).status_code == 200
            assert (await client.get(first_path, headers=second_headers)).status_code == 401
            assert (await client.get(first_path)).status_code == 401
            assert (await client.get("/api/v1/admin/tools", headers=first_headers)).status_code == 401
            assert (
                await client.delete(
                    f"/api/v1/conversations/{first.json()['id']}", headers=first_headers
                )
            ).status_code == 200
            assert (await client.get(first_path, headers=first_headers)).status_code == 401


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
