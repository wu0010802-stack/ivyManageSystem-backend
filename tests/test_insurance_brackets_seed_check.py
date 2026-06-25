"""insurance_brackets seed-presence 啟動檢查（設計審查 2026-06-25 主題 B）。

prod create_all+stamp / fresh DR DB 會漏 insurance_brackets seed → 薪資保費靜默
走 hardcode 舊年度級距（潛在錯帳）。check_insurance_brackets_seeded 在整表空時除
logger.warning 外顯式 Sentry capture_message（logger.warning 不進 Sentry），讓漏
seed 在 prod 可見。
"""

from services.insurance_service import check_insurance_brackets_seeded


class _FakeScalarResult:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val


class _FakeSession:
    """最小 session stub：execute(...).scalar() 回設定的 count。"""

    def __init__(self, count):
        self._count = count

    def execute(self, *_a, **_k):
        return _FakeScalarResult(self._count)


def test_empty_table_warns_and_pushes_sentry(monkeypatch):
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append((msg, level)),
    )
    ok = check_insurance_brackets_seeded(_FakeSession(0))
    assert ok is False
    assert len(captured) == 1, "整表空時應推剛好一則 Sentry 告警"
    assert captured[0][1] == "warning"
    assert "insurance_brackets" in captured[0][0]


def test_seeded_table_is_silent(monkeypatch):
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append((msg, level)),
    )
    ok = check_insurance_brackets_seeded(_FakeSession(82))
    assert ok is True
    assert captured == []


def test_query_failure_does_not_block_startup(monkeypatch):
    """表不存在 / 連線問題 → 回 True 不阻擋啟動（與其他 startup 檢查一致）。"""

    class _BrokenSession:
        def execute(self, *_a, **_k):
            raise RuntimeError("relation does not exist")

    assert check_insurance_brackets_seeded(_BrokenSession()) is True
