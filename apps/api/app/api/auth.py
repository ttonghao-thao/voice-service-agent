"""Per-call capability identity for the portal and injected test fixtures."""

from app.contracts import DomainError, Principal
from fastapi import Request


class Auth:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def knowledge_base_ids(self):
        return tuple(
            sorted(value.strip() for value in self.settings.knowledge_base_ids.split(",") if value.strip())
        )

    def fixture_principal(self):
        s = self.settings
        scopes = {"knowledge:read", "weather:read"}
        roles = {"operator"}
        if s.dev_admin:
            scopes.add("tools:admin")
            roles.add("admin")
        return Principal(
            user_id=s.dev_user_id,
            roles=frozenset(roles),
            scopes=frozenset(scopes),
            knowledge_base_ids=self.knowledge_base_ids(),
        )

    async def principal(self, request):
        s = self.settings
        if s.auth_mode == "fixture":
            return self.fixture_principal()
        cid = request.path_params.get("cid")
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if not cid or scheme.lower() != "bearer" or not token or len(token) > 256:
            raise DomainError("AUTH_REQUIRED", "A call access token is required", 401)
        owner_id = await self.store.owner_for_token(cid, token)
        return Principal(
            user_id=owner_id,
            roles=frozenset({"customer"}),
            scopes=frozenset({"knowledge:read"}),
            knowledge_base_ids=self.knowledge_base_ids(),
        )


async def principal(request: Request):
    return await request.app.state.auth.principal(request)
