"""Rule abstract + Violation NamedTuple."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import NamedTuple

from sqlalchemy.orm import Session


class Violation(NamedTuple):
    rule_code: str
    severity: str  # "P0" | "P1" | "P2"
    entity_type: str
    entity_id: str
    summary: str

    @property
    def dedup_key(self) -> str:
        raw = f"{self.rule_code}:{self.entity_type}:{self.entity_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Rule(ABC):
    """每條 invariant rule 的基類。"""

    code: str = ""
    severity: str = "P2"
    description: str = ""

    @abstractmethod
    def check(self, session: Session) -> list[Violation]: ...
