"""rank 14 回歸：請假 Excel 匯入支援時段欄，部分假缺時段即擋（不產孤兒假單）。

破口：匯入 row 無 start_time/end_time，部分假（時數<8）匯入後 status=pending，核准時
sync.apply 對部分假缺時段 raise → 422，成為永遠無法核准且占配額的孤兒假單。
"""

import os
import sys
from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveRecord

_HEADERS = [
    "員工編號",
    "員工姓名",
    "假別代碼",
    "開始日期",
    "結束日期",
    "時數(可空)",
    "開始時間(部分假必填HH:MM)",
    "結束時間(部分假必填HH:MM)",
    "原因(可空)",
]


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'imp.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    s = sf()
    e = Employee(employee_id="E001", name="王小明", base_salary=36000, is_active=True)
    s.add(e)
    s.commit()
    yield sf
    s.close()
    base_module._engine, base_module._SessionFactory = old_e, old_sf
    engine.dispose()


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(_HEADERS)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_partial_leave_without_time_fails_row_not_orphan(db):
    """部分假（4h）缺時段 → 該列匯入失敗、不建 pending 孤兒假單。"""
    from api.leaves import _import_leaves_sync

    content = _xlsx(
        [["E001", "王小明", "personal", "2026-09-15", "2026-09-15", 4, "", "", "半天"]]
    )
    res = _import_leaves_sync(content)
    assert res["created"] == 0, f"部分假缺時段不應建立；res={res}"
    assert res["failed"] >= 1
    assert any(
        "時間" in e for e in res["errors"]
    ), f"錯誤訊息應提示需填時段；{res['errors']}"

    with db() as s:
        assert s.query(LeaveRecord).count() == 0, "不應留下孤兒 pending 假單"


def test_partial_leave_with_time_imports_with_slots(db):
    """部分假（4h）帶時段 → 正常匯入，start_time/end_time 落地（可被核准）。"""
    from api.leaves import _import_leaves_sync

    content = _xlsx(
        [
            [
                "E001",
                "王小明",
                "personal",
                "2026-09-15",
                "2026-09-15",
                4,
                "08:00",
                "12:00",
                "上午半天",
            ]
        ]
    )
    res = _import_leaves_sync(content)
    assert res["created"] == 1, f"帶時段的部分假應成功匯入；res={res}"

    with db() as s:
        lv = s.query(LeaveRecord).one()
        assert lv.start_time == "08:00" and lv.end_time == "12:00"
        assert lv.leave_hours == 4


def test_full_day_leave_without_time_still_imports(db):
    """全日假（8h）無時段照常匯入（不受新規則影響）。"""
    from api.leaves import _import_leaves_sync

    content = _xlsx(
        [["E001", "王小明", "annual", "2026-09-15", "2026-09-15", 8, "", "", "特休"]]
    )
    res = _import_leaves_sync(content)
    assert res["created"] == 1, f"全日假應照常匯入；res={res}"
    with db() as s:
        lv = s.query(LeaveRecord).one()
        assert lv.start_time is None and lv.end_time is None
