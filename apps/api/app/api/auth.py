"""Server-owned identities for the restricted validation portal and API."""

import hashlib
import json
import re
import secrets
import time
from uuid import UUID

import jwt
from app.contracts import DomainError, Principal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/auth")
ISSUER = "voice-service-agent-local"
AUDIENCE = "voice-service-agent"
SESSION_SECONDS = 60 * 60
HASH_ITERATIONS = 310_000
HASH_FORMAT = re.compile(r"^pbkdf2_sha256:(\d+):([0-9a-f]{32}):([0-9a-f]{64})$")


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    return f"pbkdf2_sha256:{HASH_ITERATIONS}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    match = HASH_FORMAT.fullmatch(encoded)
    if not match:
        return False
    iterations, salt, expected = match.groups()
    if not 200_000 <= int(iterations) <= 2_000_000:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return secrets.compare_digest(actual, bytes.fromhex(expected))


DUMMY_PASSWORD_HASH = hash_password("unused-account")


class Auth:
    def __init__(self, settings):
        self.settings = settings
        self.accounts = {}
        if settings.auth_mode != "local":
            return
        try:
            raw_accounts = json.loads(settings.local_users_json.get_secret_value())
            if not isinstance(raw_accounts, list) or not raw_accounts:
                raise ValueError("Expected a nonempty list")
            deployment_kbs = {str(UUID(value.strip())) for value in settings.knowledge_base_ids.split(",")}
            for item in raw_accounts:
                name = item["id"]
                password_hash = item["password_hash"]
                role = item["role"]
                kbs = item["knowledge_base_ids"]
                if (
                    not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name)
                    or name in self.accounts or not isinstance(password_hash, str)
                    or not HASH_FORMAT.fullmatch(password_hash)
                    or not 200_000 <= int(HASH_FORMAT.fullmatch(password_hash).group(1)) <= 2_000_000
                    or role not in {"customer", "operator", "admin"}
                    or not isinstance(kbs, list) or not kbs
                    or not all(isinstance(kb, str) and str(UUID(kb)) in deployment_kbs for kb in kbs)
                ):
                    raise ValueError("Invalid local account")
                self.accounts[name] = {
                    "password_hash": password_hash,
                    "role": role,
                    "kbs": tuple(sorted({str(UUID(kb)) for kb in kbs})),
                }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("LOCAL_USERS_JSON contains an invalid local account") from exc

    def _principal(self, user_id: str, expires_at: int | None = None) -> Principal:
        account = self.accounts[user_id]
        role = account["role"]
        scopes = {"knowledge:read"}
        if role in {"operator", "admin"}:
            scopes.add("weather:read")
        if role == "admin":
            scopes.add("tools:admin")
        return Principal(
            user_id=user_id,
            tenant_id=self.settings.tenant_id,
            roles=frozenset({role}),
            scopes=frozenset(scopes),
            knowledge_base_ids=account["kbs"],
            expires_at=expires_at,
        )

    def login(self, username: str, password: str) -> str:
        account = self.accounts.get(username)
        verified = verify_password(password, account["password_hash"] if account else DUMMY_PASSWORD_HASH)
        if not account or not verified:
            raise DomainError("AUTH_REQUIRED", "Invalid username or password", 401)
        now = int(time.time())
        return jwt.encode(
            {
                "sub": username,
                "tenant_id": self.settings.tenant_id,
                "ver": hashlib.sha256(account["password_hash"].encode()).hexdigest(),
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": now,
                "exp": now + SESSION_SECONDS,
            },
            self.settings.auth_cookie_secret.get_secret_value(),
            algorithm="HS256",
        )

    async def principal(self, request):
        s = self.settings
        if s.auth_mode == "fixture":
            scopes = {"knowledge:read", "weather:read"}
            roles = {"operator"}
            if s.dev_admin:
                scopes.add("tools:admin")
                roles.add("admin")
            return Principal(
                user_id=s.dev_user_id,
                tenant_id=s.dev_tenant_id,
                roles=frozenset(roles),
                scopes=frozenset(scopes),
                knowledge_base_ids=tuple(sorted(x.strip() for x in s.knowledge_base_ids.split(",") if x.strip())),
            )
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.cookies.get("service_session")
        if not token:
            raise DomainError("AUTH_REQUIRED", "Please sign in", 401)
        try:
            claims = jwt.decode(
                token,
                s.auth_cookie_secret.get_secret_value(),
                algorithms=["HS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
                options={"require": ["sub", "tenant_id", "ver", "iss", "aud", "iat", "exp"]},
            )
            if claims["tenant_id"] != s.tenant_id:
                raise ValueError("Wrong tenant")
            account = self.accounts[claims["sub"]]
            version = hashlib.sha256(account["password_hash"].encode()).hexdigest()
            if not secrets.compare_digest(claims["ver"], version):
                raise ValueError("Credential changed")
            return self._principal(claims["sub"], claims["exp"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise DomainError("AUTH_REQUIRED", "Your session expired or credentials are invalid", 401) from exc


async def principal(request: Request):
    return await request.app.state.auth.principal(request)


@router.get("/me")
async def me(request: Request):
    p = await principal(request)
    return {**p.model_dump(mode="json"), "auth_mode": request.app.state.settings.auth_mode}


@router.post("/login")
async def login(request: Request, body: LoginInput):
    auth = request.app.state.auth
    if auth.settings.auth_mode != "local":
        raise DomainError("AUTH_NOT_CONFIGURED", "Local sign-in is unavailable", 503)
    token = auth.login(body.username, body.password)
    response = JSONResponse({"access_token": token, "token_type": "bearer", "expires_in": SESSION_SECONDS})
    response.set_cookie(
        "service_session", token, httponly=True, secure=True, samesite="strict", max_age=SESSION_SECONDS
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"signed_out": True})
    response.delete_cookie("service_session")
    return response
