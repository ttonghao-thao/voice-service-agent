"""Server-owned identity for the validation portal and injected test fixtures."""

from app.contracts import Principal
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/auth")


class Auth:
    def __init__(self, settings):
        self.settings = settings

    async def principal(self, request):
        s = self.settings
        knowledge_base_ids = tuple(
            sorted(value.strip() for value in s.knowledge_base_ids.split(",") if value.strip())
        )
        if s.auth_mode == "validation":
            return Principal(
                user_id="validation-customer",
                tenant_id=s.tenant_id,
                roles=frozenset({"customer"}),
                scopes=frozenset({"knowledge:read"}),
                knowledge_base_ids=knowledge_base_ids,
            )
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
            knowledge_base_ids=knowledge_base_ids,
        )


async def principal(request: Request):
    return await request.app.state.auth.principal(request)


@router.get("/me")
async def me(request: Request):
    identity = await principal(request)
    return {**identity.model_dump(mode="json"), "auth_mode": request.app.state.settings.auth_mode}
