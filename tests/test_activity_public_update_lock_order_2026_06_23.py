"""tests/test_activity_public_update_lock_order_2026_06_23.py

Code review P2（2026-06-23）：public_update 鎖多課程缺固定排序，仍有 ABBA deadlock 窗口。

register / admin-create / 家長 register 三條路徑都已補
`.order_by(ActivityCourse.id).with_for_update()` 固定批次列鎖取得順序
（見 test_activity_register_lock_order_2026_06_23.py），唯獨 public_update
（家長公開頁修改報名）的課程鎖查詢只有 `.with_for_update().all()`，沒有 order_by。
家長同時改含重疊課程組合的多筆報名時，鎖序不固定 → 尖峰期 deadlock/409 retry。

測法（與既有 lock-order 測試一致）：spy Query.with_for_update，擷取鎖定
ActivityCourse 的查詢編譯後 SQL，斷言含 `ORDER BY activity_courses.id`。
SQLite 下 FOR UPDATE 為 no-op，但 order_by 仍會編譯進語句，故可驗證鎖序已固定。
"""

import os
import sys

import sqlalchemy.orm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import ActivityCourse

# 複用 regressions 的 app fixture（auth + activity router）與 helper。
from tests.test_activity_regressions import (  # noqa: F401
    activity_client,
    _create_classroom,
    _create_course as _admin_create_course,
)

_ORDER_COL = f"{ActivityCourse.__table__.name}.{ActivityCourse.id.key}"


def _spy_course_lock_sql(monkeypatch):
    captured: list[str] = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds and cds[0]["entity"] is ActivityCourse:
            captured.append(str(self.statement))
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)
    return captured


def _assert_ordered_by_course_id(captured):
    assert captured, "未捕捉到任何鎖定 ActivityCourse 的 FOR UPDATE 查詢"
    for sql in captured:
        assert "ORDER BY" in sql, (
            "public_update 課程鎖查詢缺 ORDER BY → FOR UPDATE 列鎖取得順序不固定，"
            f"並發改報名仍有 ABBA 死鎖風險。SQL={sql}"
        )
        order_tail = sql.split("ORDER BY", 1)[1]
        assert (
            _ORDER_COL in order_tail
        ), f"public_update 課程鎖查詢未以 {_ORDER_COL} 排序固定鎖序。ORDER BY={order_tail!r}"


def _public_register(client, *, name, birthday, phone, course_names):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": name,
            "birthday": birthday,
            "parent_phone": phone,
            "class": "海豚班",
            "courses": [{"name": n, "price": "1"} for n in course_names],
            "supplies": [],
        },
    )


def test_public_update_locks_courses_ordered_by_id(activity_client, monkeypatch):
    """public_update 多課程改報名：課程 FOR UPDATE 鎖須以 id 排序固定鎖序。"""
    client, sf = activity_client
    with sf() as s:
        _create_classroom(s, "海豚班")
        _admin_create_course(s, "圍棋", 1200)
        _admin_create_course(s, "畫畫", 1500)
        _admin_create_course(s, "陶土", 1800)
        s.commit()

    # 先公開報名一筆（pending，因無對應 Student），拿 id + query_token 供修改驗證身分。
    reg_res = _public_register(
        client,
        name="王小明",
        birthday="2020-01-01",
        phone="0912345678",
        course_names=["圍棋"],
    )
    assert reg_res.status_code == 201, reg_res.text
    reg_id = reg_res.json()["id"]
    token = reg_res.json()["query_token"]
    assert reg_id and token

    captured = _spy_course_lock_sql(monkeypatch)
    res = client.post(
        "/api/activity/public/update",
        json={
            "id": reg_id,
            "name": "王小明",
            "birthday": "2020-01-01",
            "parent_phone": "0912345678",
            "query_token": token,
            "class": "海豚班",
            # 改成含重疊但不同順序的多課程，觸發多課程列鎖
            "courses": [
                {"name": "陶土", "price": "1"},
                {"name": "圍棋", "price": "1"},
                {"name": "畫畫", "price": "1"},
            ],
            "supplies": [],
        },
    )
    assert res.status_code == 200, res.text
    _assert_ordered_by_course_id(captured)
