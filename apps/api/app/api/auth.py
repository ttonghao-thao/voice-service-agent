import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import jwt
from app.contracts import DomainError, Principal
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/api/v1/auth")


class Auth:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.keys, self.expires = [], 0

    async def claims(self, token):
        s = self.settings
        try:
            if time.monotonic() > self.expires:
                response = await self.client.get(s.oidc_jwks_url, timeout=5)
                response.raise_for_status()
                self.keys = response.json()["keys"]
                self.expires = time.monotonic() + 300
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ValueError("Unsupported signing algorithm")
            key = next((k for k in self.keys if k.get("kid") == header.get("kid")), None)
            if not key:
                self.expires = 0
                raise ValueError("Unknown key")
            return jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                issuer=s.oidc_issuer,
                audience=s.oidc_audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except Exception as exc:
            raise DomainError("AUTH_REQUIRED", "登录已过期或凭据无效", 401) from exc

    async def principal(self, request):
        s = self.settings
        if s.auth_mode == "dev":
            scopes = {"knowledge:read", "weather:read"}
            if s.dev_admin:
                scopes.add("tools:admin")
            return Principal(
                user_id=s.dev_user_id,
                tenant_id=s.dev_tenant_id,
                scopes=frozenset(scopes),
                knowledge_base_ids=tuple(s.knowledge_base_ids.split(",")),
            )
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.cookies.get("service_session")
        if not token:
            raise DomainError("AUTH_REQUIRED", "请先登录", 401)
        claims = await self.claims(token)
        # Signed roles/ACL and the deployment tenant are the only identity inputs.
        if claims.get("tenant_id") != s.tenant_id:
            raise DomainError("FORBIDDEN", "无权访问此组织", 403)
        roles = claims.get("roles", [])
        kbs = claims.get("knowledge_base_ids", [])
        if not isinstance(roles, list) or not isinstance(kbs, list):
            raise DomainError("FORBIDDEN", "身份权限格式错误", 403)
        scopes = {"knowledge:read", "weather:read"} if "operator" in roles or "admin" in roles else set()
        if "admin" in roles:
            scopes.add("tools:admin")
        if not scopes:
            raise DomainError("FORBIDDEN", "没有客服工作台权限", 403)
        allowed_kbs = set(s.knowledge_base_ids.split(",")) & set(kbs)
        return Principal(
            user_id=claims["sub"],
            tenant_id=s.tenant_id,
            scopes=frozenset(scopes),
            knowledge_base_ids=tuple(sorted(allowed_kbs)),
            expires_at=claims["exp"],
        )


async def principal(request: Request):
    return await request.app.state.auth.principal(request)


@router.get("/me")
async def me(request: Request):
    p = await principal(request)
    return {**p.model_dump(mode="json"), "auth_mode": request.app.state.settings.auth_mode}


@router.get("/login")
async def login(request: Request):
    s = request.app.state.settings
    if s.auth_mode == "dev":
        return RedirectResponse(s.public_origin)
    if (
        not s.oidc_authorization_url
        or not s.oidc_token_url
        or len(s.auth_cookie_secret.get_secret_value()) < 32
    ):
        raise DomainError(
            "AUTH_NOT_CONFIGURED", "浏览器 SSO 尚未配置，可由集成方使用已验证的 Bearer token", 503
        )
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = RedirectResponse(
        s.oidc_authorization_url
        + "?"
        + urlencode(
            {
                "client_id": s.oidc_audience,
                "redirect_uri": s.public_origin + "/api/v1/auth/callback",
                "response_type": "code",
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    signed = jwt.encode(
        {"state": state, "nonce": nonce, "verifier": verifier, "exp": int(time.time()) + 300},
        s.auth_cookie_secret.get_secret_value(),
        algorithm="HS256",
    )
    response.set_cookie(
        "oidc_flow",
        signed,
        httponly=True,
        secure=s.public_origin.startswith("https"),
        samesite="lax",
        max_age=300,
        path="/api/v1/auth",
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    s = request.app.state.settings
    try:
        flow = jwt.decode(
            request.cookies.get("oidc_flow", ""),
            s.auth_cookie_secret.get_secret_value(),
            algorithms=["HS256"],
        )
        if not secrets.compare_digest(flow["state"], state) or not code:
            raise ValueError("State mismatch")
        response = await request.app.state.client.post(
            s.oidc_token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": s.oidc_audience,
                "client_secret": s.oidc_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": s.public_origin + "/api/v1/auth/callback",
                "code_verifier": flow["verifier"],
            },
        )
        response.raise_for_status()
        token = response.json()["id_token"]
        claims = await request.app.state.auth.claims(token)
        if claims.get("nonce") != flow["nonce"]:
            raise ValueError("Nonce mismatch")
    except Exception as exc:
        raise DomainError("AUTH_REQUIRED", "SSO 登录失败，请重试", 401) from exc
    redirect = RedirectResponse(s.public_origin)
    redirect.delete_cookie("oidc_flow", path="/api/v1/auth")
    redirect.set_cookie(
        "service_session",
        token,
        httponly=True,
        secure=s.public_origin.startswith("https"),
        samesite="strict",
        max_age=max(0, int(claims["exp"] - time.time())),
    )
    return redirect


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(request.app.state.settings.public_origin, status_code=303)
    response.delete_cookie("service_session")
    return response
