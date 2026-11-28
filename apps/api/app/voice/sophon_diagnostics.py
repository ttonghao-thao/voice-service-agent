"""Observe the Sophon LiveKit path without granting tool or playback authority."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from app.contracts import (
    SophonDelegationDiagnostic,
    SophonFieldIssue,
    SophonInputDiagnostic,
    SophonIntentDiagnostic,
    SophonUnassignedDiagnostic,
    uid,
)
from app.voice.delegation_router import SENSITIVE
from app.voice.sophon_live import ID

logger = logging.getLogger(__name__)
MAX_RAW_TASK = 2050
OUTPUT_PAUSE = 0.9
KNOWN_ROUTES = {"WEATHER", "CUEKB", "WEB_SEARCH", "CLARIFY", "UNSUPPORTED"}
CREDENTIAL = re.compile(
    r"(?i)\bbearer\s|\b(?:api[_ -]?key|authorization|credential|ticket|passcode|access[_ -]?key)\b"
    r"|身份证|联系方式|手机号|家庭住址|\b(?:private key|client[_ -]?secret)\b"
    r"|\b(?:sk|tvly|xox[baprs])[-_][\w-]+|\b(?:gh[pousr]_|github_pat_)[\w_]+"
    r"|\beyJ[\w-]+\.[\w-]+\.[\w-]+|://[^/\s]+@|[?&][\w-]+="
)
JSON_TYPES = {dict: "object", list: "array", str: "string", bool: "boolean",
              int: "integer", float: "number", type(None): "null"}


def visible_text(value):
    return (
        all(c.isprintable() or c in "\r\n\t" for c in value)
        and SENSITIVE.search(value) is None
        and CREDENTIAL.search(value) is None
    )


def delegation_preview(event, voice_session_id, *, private_context):
    """Preserve the known wire fields and the shape of invalid values."""
    item = SophonDelegationDiagnostic(
        interaction_id=uid(), voice_session_id=voice_session_id, structure="valid",
        native_event={"type": "session.delegation.created"},
    )

    def issue(path, expected, actual, key):
        if key not in actual:
            item.field_issues.append(SophonFieldIssue(path=path, expected=expected, received="missing"))
            return
        value = actual[key]
        length = len(value) if isinstance(value, (str, dict, list)) else None
        item.field_issues.append(SophonFieldIssue(
            path=path, expected=expected, received=JSON_TYPES.get(type(value), "unknown"), length=length,
        ))

    def preview(value, path, limit, *, private=False):
        if value is None or type(value) is bool:
            return value
        if path == "offset_ms" and type(value) is int and 0 <= value <= 7_200_000:
            return value
        if isinstance(value, str) and len(value) <= limit and not private and visible_text(value):
            return value
        item.redacted_fields.append(path)
        return {"type": JSON_TYPES.get(type(value), "unknown"),
                **({"length": len(value)} if isinstance(value, (str, dict, list)) else {})}

    item.omitted_fields = sum(key not in {"type", "delegation", "offset_ms"} for key in event)
    if "offset_ms" in event:
        item.native_event["offset_ms"] = preview(event["offset_ms"], "offset_ms", 0)
    body = event.get("delegation")
    if not isinstance(body, dict):
        issue("delegation", "object", event, "delegation")
        if "delegation" in event:
            item.native_event["delegation"] = preview(body, "delegation", 0)
    else:
        task = body.get("task")
        call_id = body.get("id")
        if isinstance(call_id, str) and ID.fullmatch(call_id):
            if visible_text(call_id):
                item.delegation_id = call_id
        else:
            issue("delegation.id", "valid identifier", body, "id")
        if body.get("type") != "delegation":
            issue("delegation.type", 'the string "delegation"', body, "type")
        if body.get("target") != "client":
            issue("delegation.target", 'the string "client"', body, "target")
        if isinstance(task, str):
            item.task_chars = len(task)
        if (not isinstance(task, str) or len(task) > MAX_RAW_TASK or not 0 < len(task.strip()) <= 2000
            or any(not char.isprintable() and char not in "\r\n\t" for char in task.strip())):
            issue("delegation.task", "nonempty task string (up to 2000 normalized characters)", body, "task")
        item.native_event["delegation"] = {
            key: preview(body[key], f"delegation.{key}", MAX_RAW_TASK if key == "task" else 128,
                         private=(key == "task" and private_context)
                         or (key == "id" and item.delegation_id is None))
            for key in ("id", "type", "target", "task") if key in body
        }
        item.omitted_fields += sum(key not in {"id", "type", "target", "task"} for key in body)
    if item.field_issues:
        item.structure = "invalid"
    return item


@dataclass
class CandidateReply:
    input_ids: set[str]
    owner_id: str | None
    last_activity: float


@dataclass
class SophonDiagnostics:
    store: object
    session: object
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending: dict = field(default_factory=dict)
    inputs: dict[str, SophonInputDiagnostic] = field(default_factory=dict)
    input_started: dict[str, float] = field(default_factory=dict)
    delegations: dict[str, SophonDelegationDiagnostic] = field(default_factory=dict)
    received_at: dict[str, float] = field(default_factory=dict)
    uncertain: set[str] = field(default_factory=set)
    reply: CandidateReply | None = None

    def publish(self, item, stage):
        line = item.model_dump(mode="json", exclude_none=True, exclude={"native_event", "intent"})
        if isinstance(item, SophonDelegationDiagnostic):
            line["elapsed_ms"] = round((time.monotonic() - self.received_at[item.interaction_id]) * 1000)
            line["tool_executed"] = any(run.operation == "adapter" and run.entered for run in item.tool_executions)
            if item.intent:
                line["intent"] = item.intent.model_dump(mode="json", exclude_none=True, exclude={"parameters"})
        elif isinstance(item, SophonInputDiagnostic):
            line["elapsed_ms"] = round((time.monotonic() - self.input_started[item.input_item_id]) * 1000)
        logger.info("sophon_interaction conversation_id=%s %s", self.session.conversation_id,
                    json.dumps({"epoch": self.session.epoch, "stage": stage, **line}, ensure_ascii=True))
        self.pending[item.interaction_id] = item
        self.changed.set()

    def input_started_at(self, item_id, started):
        if item_id not in self.inputs:
            self.input_started[item_id] = started
            self.inputs[item_id] = SophonInputDiagnostic(
                interaction_id=item_id, voice_session_id=self.session.id, input_item_id=item_id,
            )
            self.publish(self.inputs[item_id], "input_started")

    def receive(self, item, received, candidates):
        self.delegations[item.interaction_id] = item
        self.received_at[item.interaction_id] = received
        if len(candidates) == 1:
            self.bind(item, candidates[0])
        elif candidates:
            self.uncertain.update(candidate.id for candidate in candidates)
        self.publish(item, "native_received")

    def bind(self, item, owner):
        self.input_started_at(owner.id, owner.started)
        item.input_item_id = owner.id
        row = self.inputs[owner.id]
        if row.status != "delegation_received":
            row.status = "delegation_received"
            self.publish(row, "delegation_observed")

    def note(self, status, reason_code, *, persist=True):
        item = SophonUnassignedDiagnostic(
            interaction_id=uid(), voice_session_id=self.session.id, status=status, reason_code=reason_code,
        )
        if persist:
            self.publish(item, "unassigned")
        return item

    def observe_output(self, at, candidates):
        self.end_output(at)
        input_ids = {item.id for item in candidates}
        if self.reply is None:
            owner = candidates[0].id if len(candidates) == 1 else None
            if any(item.input_item_id != owner and item.status in {"received", "processing"}
                   for item in self.delegations.values()):
                owner = None
            self.reply = CandidateReply(input_ids, owner, at)
        else:
            self.reply.last_activity = at
            if input_ids - self.reply.input_ids:
                self.reply.owner_id = None
                self.reply.input_ids.update(input_ids)

    def input_resumed(self, item_id, at):
        self.end_output(at)
        if self.reply:
            self.reply.input_ids.add(item_id)
            self.reply.owner_id = None

    def end_output(self, at, *, closing=False, interrupted=False):
        reply = self.reply
        if reply is None or (not closing and at - reply.last_activity < OUTPUT_PAUSE):
            return
        self.reply = None
        if reply.owner_id and not interrupted:
            row = self.inputs.get(reply.owner_id)
            if row and not row.candidate_reply:
                row.candidate_reply = True
                self.publish(row, "candidate_reply_observed")
        elif reply.input_ids:
            self.uncertain.update(reply.input_ids)
            self.note("attribution_uncertain", "OUTPUT_INPUT_UNCERTAIN")

    def finish_input(self, item_id, *, interrupted=False, reason_code=None):
        row = self.inputs.get(item_id)
        if row is None or row.observation_complete:
            return
        if interrupted:
            row.reason_code = reason_code or "OBSERVATION_INTERRUPTED"
        if row.status != "delegation_received":
            if interrupted:
                row.status = "observation_interrupted"
            elif item_id in self.uncertain:
                row.status = "attribution_uncertain"
                row.reason_code = "INPUT_UNCERTAIN"
            elif row.candidate_reply:
                row.status = "sophon_replied"
            else:
                row.status = "no_reply_or_delegation"
        row.observation_complete = True
        self.publish(row, "input_finished")

    def route(self, item, parsed, model, *, private_context):
        decision = parsed.decision
        parameters = decision.arguments.model_dump(mode="json") if decision else None
        redacted = bool(parameters and (
            private_context or "delegation.task" in item.redacted_fields
            or any(isinstance(value, str) and not visible_text(value) for value in parameters.values())
        ))
        initial = parsed.initial_route
        item.intent = SophonIntentDiagnostic(
            initial_route=initial if initial in KNOWN_ROUTES or initial is None else "UNKNOWN",
            route=parsed.route, parse_method=parsed.parse_method,
            router_model=model if parsed.parse_method == "llm" else None,
            selected_tool=decision.tool.name if decision else None,
            parameters=None if redacted else parameters, parameters_redacted=redacted,
            reason_code=parsed.reason or None, policy_reason=parsed.policy_reason,
        )
        self.publish(item, "intent_resolved")

    def tool(self, item, observation):
        runs = {run.operation: run for run in item.tool_executions}
        runs[observation.operation] = observation
        if observation.operation == "adapter" and observation.status in {"failed", "canceled"}:
            for step, run in runs.items():
                if step != "adapter" and run.status == "started":
                    runs[step] = run.model_copy(update={"status": observation.status,
                                                        "reason_code": observation.reason_code})
        item.tool_executions = list(runs.values())
        self.publish(item, f"tool_{observation.operation}_{observation.status}")

    async def flush(self):
        if not self.pending:
            return True
        batch, self.pending = self.pending, {}
        try:
            await self.store.write_sophon_interactions(
                self.session.conversation_id, self.session.epoch, self.session.principal,
                [row.model_dump(mode="json") for row in batch.values()],
            )
            return True
        except asyncio.CancelledError:
            self.pending = {**batch, **self.pending}
            raise
        except Exception as exc:
            self.pending = {**batch, **self.pending}
            logger.warning("sophon_diagnostics_incomplete conversation_id=%s epoch=%s error_type=%s",
                           self.session.conversation_id, self.session.epoch, type(exc).__name__)
            return False
