"""Phase 3 P1 resilience：CircuitBreaker state machine + per-host singleton 行為."""

import time
import threading
import pytest


class TestCircuitBreakerStateMachine:
    def test_closed_calls_pass_through(self):
        from utils.circuit_breaker import CircuitBreaker

        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=1)
        assert b.call(lambda: "ok") == "ok"
        assert b.state == "closed"

    def test_failures_trip_to_open(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError

        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=60)
        for _ in range(3):
            with pytest.raises(ConnectionError):
                b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        assert b.state == "open"

    def test_open_rejects_without_calling_fn(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError

        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=60)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        # state now open
        called = []
        with pytest.raises(BreakerOpenError):
            b.call(lambda: called.append(1) or "ok")
        assert called == []  # fn 未被呼叫

    def test_half_open_after_recovery(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError

        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=0)  # immediate
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        time.sleep(0.01)
        # half_open 試 1 個成功 → close
        assert b.call(lambda: "ok") == "ok"
        assert b.state == "closed"

    def test_half_open_failure_reopens(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError

        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=0)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        time.sleep(0.01)
        # half_open 試一個失敗 → 再次 open
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("y")))
        assert b.state == "open"

    def test_trip_on_filter_4xx_not_tripped(self):
        """trip_on 不含 ValueError → 拋了不 trip，state 保 closed."""
        from utils.circuit_breaker import CircuitBreaker

        b = CircuitBreaker(
            "t", failure_threshold=1, recovery_seconds=60, trip_on=(ConnectionError,)
        )
        with pytest.raises(ValueError):
            b.call(lambda: (_ for _ in ()).throw(ValueError("client bug")))
        assert b.state == "closed"

    def test_success_resets_counter(self):
        from utils.circuit_breaker import CircuitBreaker

        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=60)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        # success between → reset
        b.call(lambda: "ok")
        # 再連 2 次 fail 不應 trip（counter 已 reset）
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        assert b.state == "closed"

    def test_stats_dict(self):
        from utils.circuit_breaker import CircuitBreaker

        b = CircuitBreaker("foo", failure_threshold=5, recovery_seconds=60)
        s = b.stats
        assert s["name"] == "foo"
        assert s["state"] == "closed"
        assert s["consecutive_failures"] == 0


class TestSingletons:
    def test_three_singletons_exist(self):
        from utils.circuit_breaker import (
            LINE_BREAKER,
            SUPABASE_BREAKER,
            EXTERNAL_HTTP_BREAKER,
        )

        assert LINE_BREAKER.stats["name"] == "line"
        assert SUPABASE_BREAKER.stats["name"] == "supabase"
        assert EXTERNAL_HTTP_BREAKER.stats["name"] == "external_http"

    def test_singletons_independent(self):
        from utils.circuit_breaker import LINE_BREAKER, SUPABASE_BREAKER

        for _ in range(LINE_BREAKER._failure_threshold):
            try:
                LINE_BREAKER.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
            except ConnectionError:
                pass
        assert LINE_BREAKER.state == "open"
        assert SUPABASE_BREAKER.state == "closed"
        # cleanup for other tests
        LINE_BREAKER.reset()


class TestThreadSafety:
    def test_concurrent_failures(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError

        b = CircuitBreaker("t", failure_threshold=10, recovery_seconds=60)

        def hit():
            for _ in range(5):
                try:
                    b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
                except (ConnectionError, BreakerOpenError):
                    pass

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 4 thread × 5 fail = 20 failure attempts；threshold=10 → state open
        assert b.state == "open"
