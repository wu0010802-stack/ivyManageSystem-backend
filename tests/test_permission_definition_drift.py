"""Permission enum 與 DB permission_definitions 漂移偵測（2026-06-15 運作探測 P2-2）。

Bug：rolesdb01 seed（2026-05-25）早於 6 個權限碼新增（STUDENTS_IEP_APPROVE /
  DATA_QUALITY_READ / DATA_QUALITY_WRITE / PORTAL_PREVIEW / PORTAL_IMPERSONATE /
  DSR_MANAGE），DB permission_definitions 缺碼（64 vs enum 70）→ 功能對非
  wildcard admin 鎖死、admin UI 無法授權。缺 startup drift guard 致此類漂移無人察覺。
"""

from utils.permissions import (
    Permission,
    check_permission_definition_drift,
    find_permission_definition_drift,
)

_SIX_MISSING = {
    "STUDENTS_IEP_APPROVE",
    "DATA_QUALITY_READ",
    "DATA_QUALITY_WRITE",
    "PORTAL_PREVIEW",
    "PORTAL_IMPERSONATE",
    "DSR_MANAGE",
}


def test_drift_detects_codes_missing_from_db():
    enum_codes = {p.value for p in Permission}
    db_codes = enum_codes - _SIX_MISSING  # 模擬 DB 少了 6 碼
    drift = find_permission_definition_drift(db_codes)
    assert set(drift["missing_in_db"]) == _SIX_MISSING


def test_drift_empty_when_db_in_sync():
    enum_codes = {p.value for p in Permission}
    drift = find_permission_definition_drift(enum_codes)
    assert drift["missing_in_db"] == []


def test_every_enum_value_has_a_label():
    """enum 值都應有 PERMISSION_LABELS（否則 seed/UI 缺標籤）——本身即一種漂移防護。"""
    drift = find_permission_definition_drift({p.value for p in Permission})
    assert drift["missing_label"] == [], f"enum 值缺標籤: {drift['missing_label']}"


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """最小 session stub：execute(...).all() 回 (code,) tuples。"""

    def __init__(self, codes):
        self._codes = codes

    def execute(self, *_a, **_k):
        return _FakeResult([(c,) for c in self._codes])


def test_check_drift_pushes_sentry_when_missing(monkeypatch):
    """設計審查 2026-06-25 QW3：偵測到漂移時除 logger.warning 外，須顯式 Sentry
    capture_message（logger.warning 不會被 LoggingIntegration 上報）。"""
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append((msg, level)),
    )
    enum_codes = {p.value for p in Permission}
    sess = _FakeSession(enum_codes - _SIX_MISSING)  # 模擬 DB 少 6 碼
    missing = check_permission_definition_drift(sess)
    assert set(missing) == _SIX_MISSING
    assert len(captured) == 1, "漂移時應推剛好一則 Sentry 告警"
    assert captured[0][1] == "warning"
    assert "漂移" in captured[0][0]


def test_check_drift_silent_when_in_sync(monkeypatch):
    """DB 與 enum 同步時不得推 Sentry（避免雜訊）。"""
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append((msg, level)),
    )
    sess = _FakeSession({p.value for p in Permission})
    missing = check_permission_definition_drift(sess)
    assert missing == []
    assert captured == []


def test_capture_message_is_noop_safe_when_sentry_uninitialised():
    """未 init Sentry 時 capture_message 應 no-op 不 raise（不可傳染回啟動主邏輯）。"""
    from utils.sentry_init import capture_message

    capture_message("任意告警訊息", level="warning")  # 不應 raise
