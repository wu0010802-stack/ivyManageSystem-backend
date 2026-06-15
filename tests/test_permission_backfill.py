"""permbf01 backfill 資料正確性（2026-06-15 運作探測 P2-2）。

把 migration 硬編的回填清單釘到 source of truth（Permission enum / PERMISSION_LABELS /
find_permission_definition_drift），避免 migration 與程式碼漂移。
PG 執行（INSERT + ARRAY UPDATE）另以拋棄式 PG DB 整合驗證。
"""

from utils.permission_backfill import _BACKFILL_CODES, _BACKFILL_DEFINITIONS
from utils.permissions import (
    PERMISSION_LABELS,
    Permission,
    find_permission_definition_drift,
)


def test_backfill_codes_are_valid_enum_with_matching_labels():
    enum_vals = {p.value for p in Permission}
    for code, label, group in _BACKFILL_DEFINITIONS:
        assert code in enum_vals, f"{code} 非合法 Permission enum 值"
        assert (
            PERMISSION_LABELS[code] == label
        ), f"{code} migration label 與 PERMISSION_LABELS 不符（漂移）"
        assert group, "group_name 不可為空"


def test_backfill_covers_exactly_the_known_drift():
    """模擬 DB 缺這 6 碼時，drift 偵測到的 missing 應恰為 backfill 清單。"""
    enum_vals = {p.value for p in Permission}
    db_without_backfill = enum_vals - set(_BACKFILL_CODES)
    drift = find_permission_definition_drift(db_without_backfill)
    assert set(drift["missing_in_db"]) == set(_BACKFILL_CODES)
