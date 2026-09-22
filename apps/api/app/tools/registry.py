import asyncio
import json
import time
from typing import Literal

import yaml
from app.config import ROOT
from app.contracts import DomainError, StrictModel
from app.storage.models import ToolRun
from app.tools.adapters import CueKBAdapter, WeatherAdapter
from pydantic import Field, ValidationError


class ToolSpec(StrictModel):
    name: str
    version: str
    description: str
    display_name: str
    mode_ref: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    adapter_id: str
    endpoint_ref: str
    secret_ref: str
    permission_scope: str
    timeout_ms: int = Field(ge=100, le=12000)
    read_only: Literal[True]
    enabled: bool
    allowed_tenants: list[str]
    result_limit: int = Field(ge=1024, le=32768)
    retry_policy: dict
    audit_policy: Literal["evidence_only"]


class ToolRegistry:
    def __init__(self, settings, client, store):
        self.settings, self.store = settings, store
        self.adapters = {
            "cuekb_http": CueKBAdapter(settings, client),
            "weather_http": WeatherAdapter(settings, client),
        }
        config = yaml.safe_load((ROOT / "config/tools.yaml").read_text())
        self.version = config["version"]
        self.specs = {s.name: s for s in map(ToolSpec.model_validate, config["tools"])}
        unknown_enabled_tools = settings.enabled_tool_names - self.specs.keys()
        if unknown_enabled_tools:
            raise ValueError("ENABLED_TOOLS contains unregistered tools: " + ", ".join(sorted(unknown_enabled_tools)))
        for spec in self.specs.values():
            if spec.adapter_id not in self.adapters:
                raise ValueError("Unregistered trusted adapter")
            adapter = self.adapters[spec.adapter_id]
            spec.input_schema = adapter.input_model.model_json_schema()
            spec.output_schema = adapter.output_adapter.json_schema()

    def deployment_enabled(self, name):
        return name in self.settings.enabled_tool_names and self.specs[name].enabled

    async def available(self, name):
        return self.deployment_enabled(name) and await self.store.enabled(name)

    async def allowed(self, principal):
        if principal.expires_at is not None and principal.expires_at <= time.time():
            return set()
        return {
            name
            for name, spec in self.specs.items()
            if self.deployment_enabled(name)
            and spec.permission_scope in principal.scopes
            and ("*" in spec.allowed_tenants or principal.tenant_id in spec.allowed_tenants)
            and await self.store.enabled(name)
        }

    async def invoke(self, name, arguments, ctx):
        started = time.monotonic()
        status, output = "ok", {}
        saved_evidence, saved_cards, saved_slots, saved_retrievals = (
            dict(ctx.evidence),
            list(ctx.cards),
            dict(ctx.slots),
            list(ctx.retrievals),
        )
        try:
            spec = self.specs.get(name)
            if not spec or name not in ctx.allowed_tools or name not in await self.allowed(ctx.principal):
                raise DomainError("FORBIDDEN", "This tool is unauthorized or disabled", 403)
            if not await self.store.current(
                ctx.conversation_id,
                ctx.epoch,
                ctx.turn_id,
                ctx.request_revision,
            ):
                raise DomainError("STALE_EPOCH", "This search was canceled", 409)
            if await self.store.tool_revision(name) != ctx.tool_versions.get(name, 0):
                raise DomainError("FORBIDDEN", "Tool configuration changed. Please ask again.", 403)
            adapter = self.adapters[spec.adapter_id]
            args = adapter.input_model.model_validate(arguments)
            ctx.invoked.add(name)
            async with asyncio.timeout(spec.timeout_ms / 1000):
                output = await adapter.invoke(args, ctx)
                output = adapter.output_adapter.validate_python(output).model_dump(mode="json")
            if len(json.dumps(output, ensure_ascii=False).encode()) > spec.result_limit:
                raise DomainError("TOOL_BAD_RESPONSE", "Search result is too large", 502)
            if not await self.available(name):
                raise DomainError("FORBIDDEN", "Tool was disabled during the search", 403)
            return output
        except TimeoutError:
            status = "TOOL_TIMEOUT"
            ctx.tool_errors.append(status)
            return {"status": "failed", "code": status, "message": "Search timed out. Please try again later."}
        except (ValidationError, ValueError):
            status = "TOOL_BAD_RESPONSE"
            ctx.tool_errors.append(status)
            return {"status": "failed", "code": status, "message": "Tool arguments or response violate the contract"}
        except DomainError as exc:
            status = exc.code
            ctx.tool_errors.append(status)
            return {"status": "failed", "code": status, "message": exc.message}
        except asyncio.CancelledError:
            status = "canceled"
            raise
        except Exception:
            status = "TOOL_FAILED"
            ctx.tool_errors.append(status)
            return {"status": "failed", "code": status, "message": "Search service failed. Please try again later."}
        finally:
            if status != "ok":
                ctx.evidence, ctx.cards, ctx.slots, ctx.retrievals = (
                    saved_evidence,
                    saved_cards,
                    saved_slots,
                    saved_retrievals,
                )
            async with self.store.transaction() as db:
                db.add(
                    ToolRun(
                        conversation_id=ctx.conversation_id,
                        turn_id=ctx.turn_id,
                        epoch=ctx.epoch,
                        name=name,
                        status=status,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        evidence={
                            "citations": [x.model_dump() for x in ctx.evidence.values()],
                            "cards": ctx.cards,
                        },
                    )
                )
