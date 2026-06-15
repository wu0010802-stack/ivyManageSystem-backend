"""Permission enum 與 DB permission_definitions 漂移偵測（2026-06-15 運作探測 P2-2）。

Bug：rolesdb01 seed（2026-05-25）早於 6 個權限碼新增（STUDENTS_IEP_APPROVE /
  DATA_QUALITY_READ / DATA_QUALITY_WRITE / PORTAL_PREVIEW / PORTAL_IMPERSONATE /
  DSR_MANAGE），DB permission_definitions 缺碼（64 vs enum 70）→ 功能對非
  wildcard admin 鎖死、admin UI 無法授權。缺 startup drift guard 致此類漂移無人察覺。
"""

from utils.permissions import Permission, find_permission_definition_drift

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
