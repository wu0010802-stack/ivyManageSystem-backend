"""tests/test_parent_activity_payload_limit_2026_06_29.py

第三輪才藝 review F1（高）：家長端登入報名 payload 的 course_ids/supply_ids
沒有長度上限（只去重），可被傳入數萬筆 id → 大型 IN 查詢 + 逐筆迴圈 →
DB 錯誤 / 資源耗盡。admin（AdminRegistrationPayload）與 public
（PublicRegistrationPayload / PublicUpdatePayload）的 courses/supplies 早已
限 max_length=20，唯獨家長端漏限，規則不一致。

修正：家長端 RegisterPayload.course_ids / supply_ids 補 max_length=20，
與 admin/public 對齊。
"""

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.parent_portal.activity import RegisterPayload


def _base_payload(**overrides):
    data = {
        "student_id": 1,
        "school_year": 113,
        "semester": 1,
        "course_ids": [],
        "supply_ids": [],
    }
    data.update(overrides)
    return data


def test_course_ids_over_20_rejected():
    """course_ids 逾 20 筆應被 schema 擋下（對齊 admin/public 上限）。"""
    with pytest.raises(ValidationError):
        RegisterPayload(**_base_payload(course_ids=list(range(1, 22))))


def test_supply_ids_over_20_rejected():
    """supply_ids 逾 20 筆應被 schema 擋下。"""
    with pytest.raises(ValidationError):
        RegisterPayload(**_base_payload(supply_ids=list(range(1, 22))))


def test_exactly_20_ids_accepted():
    """剛好 20 筆（去重後）仍合法，不誤殺正常上限。"""
    p = RegisterPayload(**_base_payload(course_ids=list(range(1, 21))))
    assert len(p.course_ids) == 20


def test_dedupe_still_applies_under_limit():
    """去重仍生效：原始 ≤20 筆內的重複 id 收斂保序（與 admin/public 同樣對原始
    長度設限，故上限檢查在去重之前；此處驗證去重本身未被破壞）。"""
    p = RegisterPayload(**_base_payload(course_ids=[5, 5, 6, 6, 7]))
    assert p.course_ids == [5, 6, 7]
