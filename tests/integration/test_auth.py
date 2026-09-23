import json
import time

import httpx
import jwt
import pytest
from app.api.auth import AUDIENCE, ISSUER, Auth, hash_password
from app.config import Settings
from app.contracts import DomainError
from app.main import create_app
from starlette.requests import Request

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


async def test_local_login_signed_identity_acl_and_revocation():
    users = [
        {"id": "alice", "password_hash": hash_password("alice-long-test-password"),
         "role": "customer", "knowledge_base_ids": [KB_SUPPORT]},
        {"id": "operator", "password_hash": hash_password("operator-long-test-password"),
         "role": "admin", "knowledge_base_ids": [KB_SUPPORT]},
    ]
    settings = Settings(
        _env_file=None, auth_mode="local", tenant_id="tenant-a",
        auth_cookie_secret="x" * 32, local_users_json=json.dumps(users), knowledge_base_ids=KB_SUPPORT,
    )
    auth = Auth(settings)

    def request(token):
        return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})

    token = auth.login("alice", "alice-long-test-password")
    principal = await auth.principal(request(token))
    assert principal.tenant_id == "tenant-a" and principal.knowledge_base_ids == (KB_SUPPORT,)
    assert principal.roles == frozenset({"customer"})
    assert principal.scopes == frozenset({"knowledge:read"})
    admin = await auth.principal(request(auth.login("operator", "operator-long-test-password")))
    assert "tools:admin" in admin.scopes
    with pytest.raises(DomainError):
        auth.login("alice", "wrong-password")
    with pytest.raises(DomainError):
        await auth.principal(request(token + "tampered"))

    expired = jwt.encode({"sub": "alice", "ver": "invalid", "iss": ISSUER, "aud": AUDIENCE,
                          "iat": int(time.time()) - 120, "exp": int(time.time()) - 1},
                         settings.auth_cookie_secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(DomainError):
        await auth.principal(request(expired))
    wrong_tenant_claims = jwt.decode(token, settings.auth_cookie_secret.get_secret_value(),
                                     algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    wrong_tenant_claims["tenant_id"] = "tenant-b"
    wrong_tenant = jwt.encode(wrong_tenant_claims, settings.auth_cookie_secret.get_secret_value(),
                              algorithm="HS256")
    with pytest.raises(DomainError):
        await auth.principal(request(wrong_tenant))
    users[0]["password_hash"] = hash_password("new-alice-test-password")
    changed = Auth(Settings(_env_file=None, auth_mode="local", tenant_id="tenant-a",
                            auth_cookie_secret="x" * 32, local_users_json=json.dumps(users),
                            knowledge_base_ids=KB_SUPPORT))
    with pytest.raises(DomainError):
        await changed.principal(request(token))


async def test_local_http_login_bearer_cookie_and_customer_isolation(tmp_path):
    users = [
        {"id": name, "password_hash": hash_password(name + "-long-test-password"),
         "role": "customer", "knowledge_base_ids": [KB_SUPPORT]}
        for name in ("alice", "bob")
    ]
    settings = Settings(
        _env_file=None, auth_mode="local", tenant_id="tenant-a", public_origin="https://test",
        auth_cookie_secret="x" * 32, local_users_json=json.dumps(users), knowledge_base_ids=KB_SUPPORT,
        auto_create_schema=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/local-auth.db",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as alice:
            wrong = await alice.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
            assert wrong.status_code == 401
            login = await alice.post("/api/v1/auth/login", json={
                "username": "alice", "password": "alice-long-test-password"})
            assert login.status_code == 200
            assert "httponly" in login.headers["set-cookie"].lower()
            assert "secure" in login.headers["set-cookie"].lower()
            token = login.json()["access_token"]
            assert (await alice.get("/api/v1/auth/me")).json()["user_id"] == "alice"
            created = await alice.post("/api/v1/conversations", json={"title": "private"},
                                       headers={"Origin": "https://test"})
            assert created.status_code == 201
            assert (await alice.post("/api/v1/admin/drain", json={},
                                     headers={"Origin": "https://test"})).status_code == 403
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as bob:
                login_bob = await bob.post("/api/v1/auth/login", json={
                    "username": "bob", "password": "bob-long-test-password"})
                assert login_bob.status_code == 200
                assert (await bob.get("/api/v1/conversations")).json()["items"] == []
                assert (await bob.get(f"/api/v1/conversations/{created.json()['id']}/messages")).status_code in {403, 404}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://test") as api_client:
                me = await api_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
                assert me.status_code == 200 and me.json()["user_id"] == "alice"


async def test_normal_api_startup_rejects_fixture_configuration(monkeypatch):
    monkeypatch.setattr("app.main.Settings", lambda: Settings(_env_file=None))
    app = create_app()
    with pytest.raises(ValueError, match="fixture identity"):
        async with app.router.lifespan_context(app):
            pass


def test_single_env_file_parses_local_accounts(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_USERS_JSON", raising=False)
    account = json.dumps([{
        "id": "alice", "password_hash": hash_password("alice-long-test-password"),
        "role": "customer", "knowledge_base_ids": [KB_SUPPORT],
    }], separators=(",", ":"))
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"AUTH_MODE=local\nTENANT_ID=tenant-a\nAUTH_COOKIE_SECRET={'x' * 32}\n"
        f"LOCAL_USERS_JSON='{account}'\n"
    )
    settings = Settings(_env_file=env_file)
    assert Auth(settings).login("alice", "alice-long-test-password")


def test_local_account_cannot_grant_kb_outside_deployment():
    users = [{
        "id": "alice", "password_hash": hash_password("alice-long-test-password"),
        "role": "customer", "knowledge_base_ids": ["00000000-0000-4000-8000-000000000099"],
    }]
    settings = Settings(_env_file=None, auth_mode="local", tenant_id="tenant-a",
                        local_users_json=json.dumps(users), knowledge_base_ids=KB_SUPPORT)
    with pytest.raises(ValueError, match="invalid local account"):
        Auth(settings)


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
