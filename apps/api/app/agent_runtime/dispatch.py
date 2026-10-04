"""Trusted, extensible tool registration. No semantic routing or dynamic imports."""

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.contracts import DomainError, KnowledgeArguments, StrictModel


@dataclass(frozen=True)
class NativeTool:
    name: str
    description: str
    arguments: type[StrictModel]
    executor: Callable[..., Awaitable]
    required_tools: frozenset[str] = frozenset({"search_knowledge"})
    permission_scope: str = "knowledge:read"


@dataclass(frozen=True)
class ExecutionDecision:
    tool: NativeTool
    arguments: StrictModel


class ToolDispatcher:
    def __init__(self):
        self.tools: dict[str, NativeTool] = {}

    def register(self, tool: NativeTool):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", tool.name) or tool.name in self.tools:
            raise ValueError("Native tool name must be valid and unique")
        self.tools[tool.name] = tool

    def resolve(self, name, arguments, registered):
        if name not in registered or name not in self.tools:
            raise DomainError("VOICE_TOOL_UNKNOWN", "This tool is not registered for this session", 502)
        if not isinstance(arguments, str) or len(arguments) > 12000:
            raise DomainError("VOICE_TOOL_ARGUMENTS", "Invalid tool arguments", 422)
        tool = self.tools[name]
        try:
            parsed = tool.arguments.model_validate_json(arguments)
        except ValueError as exc:
            raise DomainError("VOICE_TOOL_ARGUMENTS", "Tool arguments violate the schema", 422) from exc
        return ExecutionDecision(tool, parsed)

    def definitions(self, registered):
        return [
            {"name": name, "description": self.tools[name].description,
             "parameters": self.tools[name].arguments.model_json_schema()}
            for name in registered
        ]

    async def available(self, registry, principal):
        if principal.expires_at is not None and principal.expires_at <= time.time():
            return ()
        allowed = await registry.allowed(principal)
        return tuple(name for name, tool in self.tools.items()
                     if tool.required_tools <= allowed and tool.permission_scope in principal.scopes)


def knowledge_tools(direct, reasoned):
    dispatcher = ToolDispatcher()
    dispatcher.register(NativeTool(
        "lookup_knowledge",
        "Retrieve authorized company or product documentation for one clear factual question "
        "or documented procedure, or an explicit knowledge-base search. General concepts "
        "without company sources may be answered directly. This does not query live account "
        "or device state. For comparisons, synthesis, calculations or complex troubleshooting, "
        "use reason_over_knowledge. Preserve the complete request; never guess identifiers. "
        "The server may escalate once when evidence requires reasoning.",
        KnowledgeArguments, direct,
    ))
    dispatcher.register(NativeTool(
        "reason_over_knowledge",
        "Reason over authorized company or product documentation for comparisons, multiple "
        "versions or sources, conditional applicability, calculations or troubleshooting. "
        "Use only when company sources or an explicit knowledge-base search are required. "
        "This does not query live account or device state. Preserve the complete request "
        "and constraints. If unsure one lookup is enough, use this tool or clarify.",
        KnowledgeArguments, reasoned,
    ))
    return dispatcher
