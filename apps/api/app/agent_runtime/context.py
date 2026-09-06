from dataclasses import dataclass, field
from typing import Any

from app.contracts import Citation, Principal, uid


@dataclass
class RunContext:
    principal: Principal
    conversation_id: str
    turn_id: str
    epoch: int
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    run_id: str = field(default_factory=uid)
    evidence: dict[str, Citation] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)
    invoked: set[str] = field(default_factory=set)
    tool_errors: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    tool_versions: dict[str, int] = field(default_factory=dict)
    allowed_tools: set[str] = field(default_factory=set)
