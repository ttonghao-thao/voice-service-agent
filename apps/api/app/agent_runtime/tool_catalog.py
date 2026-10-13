"""Global, persistent native-tool metadata; executable code stays trusted."""

import json
from dataclasses import dataclass, replace

import yaml
from app.config import ROOT
from app.contracts import DomainError, StrictModel, now, printable_ascii
from app.storage.models import AdminAudit, NativeToolConfig
from pydantic import Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError


class NativeToolEntry(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    implementation: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    description: str = Field(min_length=1, max_length=1600)
    enabled: bool = True
    ack_messages: list[str] = Field(default_factory=lambda: ["Please wait while I check that."], min_length=1, max_length=2)
    examples: list[dict] = Field(default_factory=list, max_length=3)
    output_description: str = Field(default="", max_length=600)

    @field_validator("description", "output_description")
    @classmethod
    def ascii_text(cls, value):
        if not printable_ascii(value):
            raise ValueError("VoiceChat metadata must be ASCII")
        return value

    @field_validator("ack_messages")
    @classmethod
    def acknowledgments(cls, values):
        if any(not value.strip() or len(value) > 160 or not printable_ascii(value) for value in values):
            raise ValueError("ACK messages must be nonempty ASCII, at most 160 characters")
        return [" ".join(value.split()) for value in values]


class NativeToolCatalogUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    items: list[NativeToolEntry] = Field(max_length=64)


class NativeToolReload(StrictModel):
    expected_revision: int = Field(ge=0)


@dataclass(frozen=True)
class NativeToolSnapshot:
    revision: int
    tools: dict
    definitions: list


class NativeToolCatalog:
    def __init__(self, dispatcher, registry):
        self.dispatcher, self.registry = dispatcher, registry
        self.store = registry.store
        self.defaults = self.load_file()

    def load_file(self):
        payload = yaml.safe_load((ROOT / "config/voice-tools.yaml").read_text())
        entries = [NativeToolEntry.model_validate(value) for value in payload["tools"]]
        self.compile(entries)
        return entries

    def compile(self, entries):
        if len(entries) > 64:
            raise DomainError("NATIVE_TOOL_CONFIGURATION", "The native tool catalog exceeds its 64-entry budget", 422)
        tools = {}
        for entry in entries:
            implementation = self.dispatcher.tools.get(entry.implementation)
            if entry.name in tools or implementation is None:
                raise DomainError("NATIVE_TOOL_CONFIGURATION", "Tool names must be unique and implementations must be registered", 422)
            if not implementation.required_tools <= self.registry.specs.keys():
                raise DomainError("NATIVE_TOOL_CONFIGURATION", "A native tool requires an unknown backend tool", 422)
            if not printable_ascii(json.dumps(implementation.arguments.model_json_schema(), ensure_ascii=False)):
                raise DomainError("NATIVE_TOOL_CONFIGURATION", "Trusted parameter schemas must be ASCII", 422)
            examples = []
            for value in entry.examples:
                try:
                    parsed = implementation.arguments.model_validate(value)
                except ValueError as exc:
                    raise DomainError("NATIVE_TOOL_CONFIGURATION", "Tool examples must conform to the trusted parameter schema", 422) from exc
                encoded = json.dumps(parsed.model_dump(), ensure_ascii=False, sort_keys=True)
                if not printable_ascii(encoded) or len(encoded) > 2000:
                    raise DomainError("NATIVE_TOOL_CONFIGURATION", "Tool examples must be bounded ASCII", 422)
                examples.append(encoded)
            description = entry.description
            if examples:
                description += "\nExample arguments: " + "; ".join(examples)
            if entry.output_description:
                description += "\nResult: " + entry.output_description
            tools[entry.name] = replace(implementation, name=entry.name, description=description,
                                       ack_messages=tuple(entry.ack_messages))
        definitions = self.dispatcher.definitions(tuple(tools), catalog=tools)
        if len(json.dumps(definitions).encode()) > 65536:
            raise DomainError("NATIVE_TOOL_CONFIGURATION", "The native tool catalog exceeds its 64 KiB budget", 422)
        return tools

    async def configuration(self):
        async with self.store.sessions() as db:
            row = await db.get(NativeToolConfig, 1)
            if row is None:
                return 0, self.defaults
            return row.revision, [NativeToolEntry.model_validate(value) for value in row.items]

    async def revision(self):
        async with self.store.sessions() as db:
            revision = await db.scalar(select(NativeToolConfig.revision).where(NativeToolConfig.id == 1))
            return revision or 0

    async def snapshot(self, wait_interaction=False):
        revision, entries = await self.configuration()
        compiled = self.compile(entries)
        # Visibility is global. Authorization is still checked when executing.
        enabled = {}
        for entry in entries:
            tool = compiled[entry.name]
            if entry.enabled and all([await self.registry.available(name) for name in tool.required_tools]):
                enabled[entry.name] = tool
        return NativeToolSnapshot(revision, enabled,
            self.dispatcher.definitions(tuple(enabled), wait_interaction, enabled))

    async def update(self, entries, expected_revision, user_id, action="native_tools.updated"):
        self.compile(entries)  # Validate the entire candidate before touching live configuration.
        try:
            async with self.store.transaction() as db:
                row = await db.get(NativeToolConfig, 1, with_for_update=True)
                revision = row.revision if row else 0
                if revision != expected_revision:
                    raise DomainError("STALE_TOOL_CONFIGURATION", "Tool configuration changed. Reload before saving.", 409)
                values = [entry.model_dump() for entry in entries]
                if row:
                    result = await db.execute(update(NativeToolConfig).where(
                        NativeToolConfig.id == 1, NativeToolConfig.revision == expected_revision
                    ).values(items=values, revision=revision + 1, updated_at=now()))
                    if result.rowcount != 1:
                        raise DomainError("STALE_TOOL_CONFIGURATION", "Tool configuration changed. Reload before saving.", 409)
                else:
                    db.add(NativeToolConfig(id=1, revision=1, items=values))
                db.add(AdminAudit(user_id=user_id, action=action,
                    details={"revision": revision + 1, "names": [entry.name for entry in entries]}))
        except IntegrityError as exc:
            raise DomainError("STALE_TOOL_CONFIGURATION", "Another administrator saved the catalog first.", 409) from exc
        return revision + 1
