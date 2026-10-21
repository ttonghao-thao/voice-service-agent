"""Bounded assembly of explicitly declared native model tool batches."""

import time
from dataclasses import dataclass, field

from app.contracts import DomainError


@dataclass
class ToolBatch:
    id: str | None
    input_id: str
    size: int
    after_terminal: bool
    response_id: str | None
    started: float = field(default_factory=time.monotonic)
    calls: list[dict] = field(default_factory=list)


class ToolBatches:
    def __init__(self):
        self.pending = {}
        self.closed = set()

    def append(self, payload, input_id, after_terminal):
        bid = payload.get("native_batch_id")
        if bid is None:
            return ToolBatch(None, input_id, 1, after_terminal, payload.get("response_id"), calls=[payload])
        if bid in self.closed:
            raise DomainError("VOICE_PROTOCOL_ERROR", "Native batch was already completed", 502)
        batch = self.pending.get(bid)
        if batch is None:
            if len(self.pending) >= 16:
                raise DomainError("VOICE_TOOL_LIMIT", "Too many incomplete tool batches", 502)
            batch = ToolBatch(bid, input_id, payload["native_batch_size"], after_terminal, payload.get("response_id"))
            self.pending[bid] = batch
        if (batch.input_id != input_id or batch.size != payload["native_batch_size"]
            or batch.response_id != payload.get("response_id") or payload["output_index"] != len(batch.calls)):
            raise DomainError("VOICE_PROTOCOL_ERROR", "Inconsistent native tool batch", 502)
        batch.calls.append(payload)
        if len(batch.calls) == batch.size:
            del self.pending[bid]
            self.closed.add(bid)
            return batch
        return None

    def check_timeout(self):
        if any(time.monotonic() - batch.started > 5 for batch in self.pending.values()):
            raise DomainError("VOICE_PROTOCOL_ERROR", "Native tool batch is incomplete", 502)
