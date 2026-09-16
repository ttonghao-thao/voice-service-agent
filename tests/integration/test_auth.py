import json
import time

import httpx
import jwt
import pytest
from app.api.auth import Auth
from app.config import Settings
from app.contracts import DomainError
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


async def test_oidc_signed_acl_tenant_and_expiration():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk["kid"] = "fixture-key"
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        tenant_id="tenant-a",
        oidc_issuer="https://idp.invalid",
        oidc_audience="portal",
        oidc_jwks_url="https://idp.invalid/keys",
        knowledge_base_ids=KB_SUPPORT,
    )
    claims = {
        "sub": "alice",
        "iss": settings.oidc_issuer,
        "aud": "portal",
        "exp": int(time.time()) + 60,
        "iat": int(time.time()),
        "tenant_id": "tenant-a",
        "roles": ["operator"],
        "knowledge_base_ids": [KB_SUPPORT, "00000000-0000-4000-8000-000000000099"],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))
    ) as client:
        auth = Auth(settings, client)

        def request(payload):
            token = jwt.encode(payload, private, algorithm="RS256", headers={"kid": "fixture-key"})
            return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})

        principal = await auth.principal(request(claims))
        assert principal.tenant_id == "tenant-a" and principal.knowledge_base_ids == (KB_SUPPORT,)
        assert principal.roles == frozenset({"operator"})
        assert "tools:admin" not in principal.scopes
        customer = await auth.principal(request({**claims, "roles": ["customer"]}))
        assert customer.scopes == frozenset({"knowledge:read"})
        assert customer.roles == frozenset({"customer"})
        with pytest.raises(DomainError):
            await auth.principal(request({**claims, "tenant_id": "tenant-b"}))
        with pytest.raises(DomainError):
            await auth.principal(request({**claims, "exp": int(time.time()) - 1}))
        with pytest.raises(DomainError):
            await auth.principal(request({**claims, "aud": "other-application"}))


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
