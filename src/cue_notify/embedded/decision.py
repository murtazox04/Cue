from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from cue_notify.core.enums import Importance

QUIET_HOURS = "quiet_hours"
SCHEDULED = "scheduled"
DIGEST = "digest"


class Action(StrEnum):
    SEND = "send"
    DEFER = "defer"
    SUPPRESS = "suppress"
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    action: Action
    at: datetime
    reason: str | None
    person: str
    category: str
    importance: Importance
    sender: str | None = None
    key: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict[str, Any])
    items: tuple[Decision, ...] = ()
    count: int = 1
    sent_at: datetime | None = None
