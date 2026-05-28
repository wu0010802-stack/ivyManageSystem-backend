"""utils/circuit_breaker.py — 純 in-process circuit breaker (Phase 3 P1 resilience).

CLOSED → OPEN → HALF_OPEN → CLOSED state machine。Per-worker state，
不上 Redis 分散式（YAGNI；每 worker 獨立觀察獨立 trip 是設計選擇）。

對應 spec §4.2 + §7。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Literal, TypeVar

import requests as _requests

logger = logging.getLogger(__name__)

T = TypeVar("T")
StateType = Literal["closed", "open", "half_open"]

_HTTP_TRANSIENT_EXC = (
    _requests.exceptions.ConnectionError,
    _requests.exceptions.Timeout,
    _requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)


class BreakerOpenError(Exception):
    """Caller 知道是 breaker 拒絕；不是真的失敗。Caller 自決後備行為。"""


class CircuitBreaker:
    """Simple in-process state machine.

    Args:
        name: identifier (for stats / logging)
        failure_threshold: 連續多少次失敗才 trip 到 OPEN
        recovery_seconds: OPEN 多久後進 HALF_OPEN 試探
        trip_on: 只有這些 type 的 exception 才算失敗；None = 全部算
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: int = 60,
        trip_on: tuple[type[BaseException], ...] | None = None,
    ):
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._trip_on = trip_on
        self._lock = threading.Lock()
        self._state: StateType = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> StateType:
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return self._state

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self._name,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "failure_threshold": self._failure_threshold,
                "recovery_seconds": self._recovery_seconds,
            }

    def reset(self) -> None:
        """Test helper：清乾淨狀態。Prod code 不該呼叫。"""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None

    def call(self, fn: Callable[[], T]) -> T:
        """執行 fn；OPEN state 直接拋 BreakerOpenError。"""
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            if self._state == "open":
                raise BreakerOpenError(
                    f"breaker '{self._name}' is open "
                    f"(consecutive_failures={self._consecutive_failures})"
                )

        try:
            result = fn()
        except BaseException as exc:
            # 是否算 trip 條件
            if self._trip_on is None or isinstance(exc, self._trip_on):
                with self._lock:
                    self._consecutive_failures += 1
                    was_half_open = self._state == "half_open"
                    if (
                        was_half_open
                        or self._consecutive_failures >= self._failure_threshold
                    ):
                        self._state = "open"
                        # half_open re-trip：確保至少 1 ms 後才再進 HALF_OPEN
                        # 避免 recovery_seconds=0 時 state 屬性立即又翻回 half_open
                        self._opened_at = time.time() + (0.001 if was_half_open else 0)
                        logger.warning(
                            "circuit breaker '%s' tripped to OPEN (failures=%s)",
                            self._name,
                            self._consecutive_failures,
                        )
            raise

        # success
        with self._lock:
            self._consecutive_failures = 0
            if self._state == "half_open":
                self._state = "closed"
                self._opened_at = None
                logger.info("circuit breaker '%s' recovered to CLOSED", self._name)

        return result

    def _maybe_transition_to_half_open_locked(self) -> None:
        """Called under lock. OPEN + 時間到 → HALF_OPEN（接受 1 個試探）."""
        if self._state == "open" and self._opened_at is not None:
            if (time.time() - self._opened_at) >= self._recovery_seconds:
                self._state = "half_open"
                logger.info("circuit breaker '%s' entering HALF_OPEN", self._name)


# Module-level singletons
LINE_BREAKER = CircuitBreaker(
    "line",
    failure_threshold=5,
    recovery_seconds=60,
    trip_on=_HTTP_TRANSIENT_EXC,
)
SUPABASE_BREAKER = CircuitBreaker(
    "supabase",
    failure_threshold=5,
    recovery_seconds=60,
    trip_on=_HTTP_TRANSIENT_EXC,
)
EXTERNAL_HTTP_BREAKER = CircuitBreaker(
    "external_http",
    failure_threshold=10,
    recovery_seconds=120,
    trip_on=_HTTP_TRANSIENT_EXC,
)
