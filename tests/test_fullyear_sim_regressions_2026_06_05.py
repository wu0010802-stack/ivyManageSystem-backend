"""全年度營運模擬(2026-06-05)抓到的系統 500 bug 回歸測試。

每個 test 都「重現」一個修復前會 500 / AttributeError / ValidationError 的缺陷:
修復前會失敗、修復後通過。對應 commit 的 6 處修補。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest


# ── A1: portal/_shared.add_last_modified_header 不可對 naive datetime 崩潰 ──────
def test_add_last_modified_header_accepts_naive_datetime() -> None:
    """naive 台北 datetime 餵 format_datetime(usegmt=True) 修復前 raise ValueError。"""
    from fastapi.responses import Response
    from api.portal._shared import add_last_modified_header

    resp = Response()
    add_last_modified_header(resp, datetime(2026, 3, 16, 9, 30, 0))  # naive
    hdr = resp.headers["Last-Modified"]
    assert hdr.endswith("GMT")  # 合法 HTTP-date
    # 台北 09:30 → UTC 01:30
    assert "01:30:00" in hdr


# ── A2: 學生歷程時間軸 payment/transfer/funnel 三型必須帶 payload(schema 必填)──
def test_timeline_payment_item_has_payload() -> None:
    from services.student_records_timeline import _build_payment_item

    payment = SimpleNamespace(id=1, amount=350, payment_date=datetime(2026, 2, 21))
    item = _build_payment_item((payment, 7, "114下學期 保險費"))
    assert "payload" in item
    assert item["payload"]["amount"] == 350


def test_timeline_transfer_item_has_payload() -> None:
    from services.student_records_timeline import _build_classroom_transfer_item

    tr = SimpleNamespace(
        id=2,
        student_id=7,
        transferred_at=datetime(2026, 2, 1, 9, 0),
        from_classroom_id=3,
        to_classroom_id=4,
        transferred_by=2,
    )
    item = _build_classroom_transfer_item(tr)
    assert "payload" in item
    assert item["payload"]["to_classroom_id"] == 4


def test_timeline_funnel_item_has_payload() -> None:
    from services.student_records_timeline import _build_funnel_event_item

    fe = SimpleNamespace(
        id=3,
        student_id=7,
        from_stage="visited",
        to_stage="deposited",
        created_at=datetime(2026, 1, 5, 10, 0),
        reason="家長確認報名",
        actor_user_id=2,
        event_type="stage_change",
    )
    item = _build_funnel_event_item(fe)
    assert "payload" in item
    assert item["payload"]["to_stage"] == "deposited"


# ── A3: 市場情報小樣本 deposit_rate_90d=None 必須通過 schema(業主決議)──────────
def test_market_district_row_allows_null_deposit_rate() -> None:
    """visit_90d < 10 時 service 回 deposit_rate_90d=None;schema 修復前為必填 float。"""
    from schemas.recruitment_market import RecruitmentMarketDistrictRowOut

    row = RecruitmentMarketDistrictRowOut(
        district="鳥松區",
        lead_count_30d=2,
        lead_count_90d=5,
        deposit_rate_90d=None,  # 小樣本抑制
        data_completeness="low",
    )
    assert row.deposit_rate_90d is None


# ── A4: 競品 geocode-pending 的 CompetitorSchool import 路徑 ───────────────────
def test_competitor_school_import_path() -> None:
    """修復前 from models.competitor_school import CompetitorSchool → ModuleNotFoundError。"""
    from models.recruitment import CompetitorSchool  # noqa: F401

    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("models.competitor_school")


# ── A5: portal 個資匯出 _collect_profile 須用現行 Employee 欄位(非 termination_date/status)──
def test_collect_profile_uses_current_employee_fields() -> None:
    """修復前讀 emp.termination_date / emp.status(皆不存在)→ AttributeError 500。"""
    from api.portal.data_export import _collect_profile

    emp = SimpleNamespace(
        id=60,
        name="測試員工",
        id_number=None,
        phone=None,
        email=None,
        address=None,
        hire_date=None,
        resign_date=None,
        is_active=True,
        bank_account=None,
        emergency_contact_name=None,
        emergency_contact_phone=None,
    )
    profile = _collect_profile(emp)
    assert profile["status"] == "在職"
    assert profile["termination_date"] is None
