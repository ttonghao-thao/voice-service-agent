"""Bounded search state for one input in one native voice connection."""

import json
from dataclasses import dataclass, field

from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import voice_result
from app.contracts import DomainError, uid

# Stable evidence/ASCII/negation policy is registered once in voice-qa-prompt.txt.
# Function-channel tokens are consumed one model step at a time: repeat only
# the per-result coverage/action reminder, not the full session policy.
ANSWER_INSTRUCTIONS = (
    "Answer EVERY requested detail in order from these excerpts, or explicitly say that detail "
    "cannot be confirmed here. Keep numbers and conditions. "
)
FINAL_INSTRUCTIONS = "Search finished. Do not call another tool. Answer now."


@dataclass
class NativeInputSearch:
    limit: int
    queries: set[str] = field(default_factory=set)
    ready: EvidenceReady | None = None
    versions: dict[str, int] = field(default_factory=dict)
    terminal_reason: str | None = None
    duplicate_queries: int = 0
    executions: int = 0
    result_overhead: int = 0

    def reserve(self, name, arguments):
        if self.terminal_reason:
            raise DomainError("VOICE_TOOL_LOOP", "The model requested another search after search was finished.", 502, True)
        key = json.dumps([name, arguments], sort_keys=True, ensure_ascii=True)
        if key in self.queries:
            self.duplicate_queries += 1
            self.terminal_reason = "duplicate_query"
            return False
        if len(self.queries) >= self.limit:
            raise DomainError("VOICE_TOOL_LOOP", "The search budget for this question is exhausted.", 502, True)
        self.queries.add(key)
        if len(self.queries) == self.limit:
            self.terminal_reason = "budget_exhausted"
        return True

    async def authorize(self, runtime, ctx, decision):
        # Cached evidence is scoped to this connection/input, but permission and
        # backend revisions must still be checked before reuse and publication.
        expected = {**self.versions, **{name: ctx.tool_versions[name] for name in ctx.invoked}}
        if any([await runtime.registry.store.tool_revision(name) != version for name, version in expected.items()]):
            raise DomainError("FORBIDDEN", "Knowledge configuration changed.", 403)
        await runtime.prepare(ctx)
        if (not decision.tool.required_tools <= ctx.allowed_tools
            or decision.tool.permission_scope not in ctx.principal.scopes
            or any(ctx.tool_versions.get(name) != version for name, version in self.versions.items())):
            raise DomainError("FORBIDDEN", "Knowledge access or configuration changed.", 403)
        ctx.invoked.update(self.versions)
        ctx.selected_tool = decision.tool.name
        ctx.tool_arguments = decision.arguments.model_dump()
        ctx.effective_executor = "native_cuekb"

    def combine(self, bundle, ctx, max_bytes):
        max_bytes -= self.result_overhead
        previous = self.ready
        reused_after_empty = False
        self.executions += ctx.retrieval_calls
        self.versions.update({name: ctx.tool_versions[name] for name in ctx.invoked})
        if not isinstance(bundle, EvidenceReady):
            self.terminal_reason = self.terminal_reason or "search_unavailable"
            # An empty supplementary search must not erase earlier valid facts.
            # Authorization failures, conflicting evidence and other failures
            # are not converted into successful cached answers.
            if not previous or bundle.reason_code != "CUEKB_NO_EVIDENCE":
                return bundle
            bundle = previous
            reused_after_empty = True
        envelope = {**bundle.envelope, "items": [], "answer_id": uid(),
                    "partial": bool(bundle.envelope["partial"] or (previous and previous.envelope["partial"])
                                    or reused_after_empty)}
        # Reserve the full final instruction even on the first result so that
        # ending search never displaces facts already admitted for this input.
        envelope["instructions"] = ANSWER_INSTRUCTIONS + FINAL_INSTRUCTIONS
        envelope["search_state"] = "finished"
        merged = EvidenceReady(envelope, [], ctx.deadline, envelope["answer_id"])
        seen = set()
        for evidence in ([previous, bundle] if previous else [bundle]):
            for item, citation in zip(evidence.envelope["items"], evidence.citations, strict=True):
                key = json.dumps({k: v for k, v in item.items() if k != "citation_id"}, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                citation_id = f"C{len(merged.citations) + 1}"
                candidate = {**item, "citation_id": citation_id}
                envelope["items"].append(candidate)
                if len(json.dumps(voice_result(merged), ensure_ascii=True).encode()) > max_bytes:
                    envelope["items"].pop()
                    envelope["partial"] = True
                    continue
                merged.citations.append(citation.model_copy(update={"citation_id": citation_id}))
        if not merged.citations:
            raise DomainError("VOICE_EVIDENCE_BUDGET", "No complete evidence fits the spoken result budget.", 502, True)
        if not self.terminal_reason:
            envelope["search_state"] = "available"
            envelope["instructions"] = ANSWER_INSTRUCTIONS + "Search again only for a missing requested detail."
        self.ready = merged
        ctx.evidence = {c.citation_id: c for c in merged.citations}
        return merged
