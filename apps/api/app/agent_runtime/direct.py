import logging
import re

from app.agent_runtime.context import BusinessInput
from app.contracts import KnowledgeBundle

logger = logging.getLogger(__name__)


def confirmed_filter(value, text, slot):
    normalized = " ".join(value.split()).lower()
    if slot and normalized == " ".join(str(slot).split()).lower():
        return True
    # Connected model/version components are a single token. A sentence-ending
    # dot is punctuation, while a dot followed by another component is not.
    pattern = r"(?<![a-z0-9_./-])" + re.escape(normalized) + r"(?![a-z0-9_/-]|\.[a-z0-9])"
    return re.search(pattern, " ".join(text.split()).lower()) is not None


class DirectKnowledgeExecutor:
    client = None

    def __init__(self, settings, registry):
        self.registry = registry
        self.is_mock = settings.cuekb_mode == "mock"

    async def run(self, request: BusinessInput, ctx, history, progress=None):
        ctx.allowed_tools = await self.registry.allowed(ctx.principal)
        ctx.tool_versions = {
            name: await self.registry.store.tool_revision(name) for name in ctx.allowed_tools
        }
        base = dict(
            authorized_kb_ids=list(ctx.principal.knowledge_base_ids),
            tool_version=ctx.tool_versions.get("search_knowledge"),
            retrieval_status=None,
            evidence_status=None,
        )
        if "search_knowledge" not in ctx.allowed_tools:
            return KnowledgeBundle(
                **base,
                directive="report_failure",
                reason_code="AGENT_NO_AUTHORIZED_TOOL",
                message="No authorized knowledge search is available.",
            )
        query = request.knowledge_query
        if query is None:
            return KnowledgeBundle(
                **base,
                directive="report_failure",
                reason_code="VOICE_INVALID_QUERY",
                message="Please repeat the completed request.",
            )
        args = query.model_dump()
        for field in ("product_model", "software_version"):
            value, slot = args[field], ctx.slots.get(field)
            if value and not confirmed_filter(value, request.user_text, slot):
                return KnowledgeBundle(
                    **base,
                    directive="ask_clarification",
                    reason_code="KNOWLEDGE_FILTER_UNCONFIRMED",
                    message="Please confirm the product model or software version.",
                )
            if value is None and slot:
                args[field] = slot
        if progress:
            await progress("Searching Knowledge base")
        result = await self.registry.invoke("search_knowledge", args, ctx)
        allowed = await self.registry.allowed(ctx.principal)
        if (
            ctx.tool_errors
            or "search_knowledge" not in allowed
            or (await self.registry.store.tool_revision("search_knowledge") != base["tool_version"])
        ):
            return KnowledgeBundle(
                **base,
                directive="report_failure",
                reason_code=ctx.tool_errors[0] if ctx.tool_errors else "FORBIDDEN",
                message="Knowledge search is unavailable. Please try again later.",
            )
        retrieval, evidence = result["status"], result["evidence_status"]
        citations = sorted(ctx.evidence.values(), key=lambda c: c.rank)
        directive, reason, message = "answer_from_evidence", None, ""
        if retrieval == "needs_clarification":
            directive, message = "ask_clarification", "Please clarify the knowledge question."
        elif retrieval == "not_found" or evidence in ("insufficient", "conflicting") or not citations:
            directive, reason, message = (
                "report_insufficient",
                "CUEKB_NO_EVIDENCE",
                "The available knowledge is insufficient or conflicting.",
            )
        base.update(retrieval_status=retrieval, evidence_status=evidence)
        logger.info(
            "direct_knowledge_finished conversation_id=%s turn_id=%s mode=direct model_calls=0",
            ctx.conversation_id,
            ctx.turn_id,
        )
        return KnowledgeBundle(
            **base,
            directive=directive,
            reason_code=reason,
            message=message,
            citations=citations,
            trace_id=result["trace_id"],
            degraded_reasons=result.get("degraded_reasons", []),
            scope_limited=result.get("scope_limited", False),
            application_limited=any(r.get("application_limited") for r in ctx.retrievals),
            hits_omitted=result.get("hits_omitted", 0),
            is_mock=self.is_mock,
        )

    async def close(self):
        pass
