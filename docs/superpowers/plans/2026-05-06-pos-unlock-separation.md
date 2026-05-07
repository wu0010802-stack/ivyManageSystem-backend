# POS 日結解鎖權限分離（Unlock 4-eye）實作計劃

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** POS 日結 unlock 端點強制 4-eye（解鎖人 ≠ 原簽核人），admin role 可緊急 override 並 LINE 通知；approve 加軟提醒；新增異常稽核 dashboard。

**Architecture:** 後端在 `pos_approval.py` schema/handler 加守衛，沿用 `ApprovalLog.action` 區分 `cancelled` vs `admin_override`；`line_service` 加 best-effort 通知方法；新 audit endpoint。前端 `POSApprovalView` unlock 三分支 UI + 新 `POSAuditEventsView` timeline + 路由 + 入口連結。**無 DB migration**。

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic v2 (backend), Vue 3 + Element Plus + Pinia (frontend), pytest (backend tests), vitest (frontend tests)

**前置 Spec:** `/Users/yilunwu/Desktop/ivy-backend/docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md`

**Branch:** 沿用 `fix/bug-sweep-v1`（cash-only 已在此分支完成兩個 commit）；commit 用具名 file 避免帶到 baseline WIP。

---

## 執行順序總覽

| Phase | Task | 內容 | Repo |
|-------|------|------|------|
| A | 1 | unlock schema + 4-eye 守衛 + ApprovalLog action 區分 + 5 個測試 | backend |
| A | 2 | LINE 通知方法 + best-effort exception + 1 個測試 | backend |
| A | 3 | approve warnings + 2 個測試 | backend |
| A | 4 | audit endpoint + 2 個測試 | backend |
| A | 5 | 修舊測試 fixture + 後端 final commit | backend |
| B | 6 | 前端 API 模組調整 + POSApprovalView unlock 三分支 | frontend |
| B | 7 | 前端 POSApprovalView approve warnings + audit 入口連結 | frontend |
| B | 8 | 新 view POSAuditEventsView + 路由 | frontend |
| B | 9 | 前端 final commit | frontend |
| C | 10 | 整合驗證 (golden path) | both |

**重要：** 所有後端測試走 `cd /Users/yilunwu/Desktop/ivy-backend && pytest`；前端 `cd /Users/yilunwu/Desktop/ivy-frontend && npm test`。後端與前端各一筆 commit。

---

## Task 1: unlock schema + 4-eye 守衛

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos_approval.py`（DailyCloseUnlock schema、unlock_daily_close handler）
- Create: `tests/test_pos_unlock_separation.py`

**Spec ref:** §0 成功標準 1-5、§1.1

### - [ ] Step 1: 寫失敗測試

建立 `/Users/yilunwu/Desktop/ivy-backend/tests/test_pos_unlock_separation.py`：

```python
"""
test_pos_unlock_separation.py — 驗證 POS 日結 unlock 4-eye + admin override。

對齊 spec: docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityPosDailyClose, ApprovalLog, Base
from utils.permissions import Permission

# 引用既有 helper
from tests.test_activity_pos import (
    _add_payment,
    _create_admin,
    _login,
    _make_reg_minimal,
)


APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def unlock_client(tmp_path):
    """提供 client + session_factory；同 pos_client 模式，但獨立 fixture
    避免污染既有測試。"""
    db_path = tmp_path / "unlock.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _approve_a_day(client, session_factory, *, target, approver_username):
    """Helper：以 approver_username 簽核 target 日。"""
    res = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"note": "test approve"},
    )
    assert res.status_code == 201, res.text


def _seed_signed_close(session_factory, *, target, approver_username, role="staff"):
    """Helper：直接在 DB 寫入一筆已簽核的 ActivityPosDailyClose。"""
    from datetime import datetime
    with session_factory() as s:
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username=approver_username,
                approved_at=datetime.now(),
                payment_total=1000,
                refund_total=0,
                net_total=1000,
                transaction_count=1,
                by_method_json='{"現金": 1000}',
            )
        )
        s.commit()


# ── Test 1-5: unlock 4-eye 守衛 ──────────────────────────────────────


def test_unlock_by_original_approver_rejected_403(unlock_client):
    """原簽核人不可解鎖自己簽過的日子（一般 4-eye 路徑）。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_a").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "想自己解鎖看看會不會被擋", "is_admin_override": False},
    )
    assert res.status_code == 403, res.text
    assert "原簽核人" in res.json()["detail"]


def test_unlock_by_other_approver_succeeds(unlock_client):
    """不同 PAYMENT_APPROVE 持有者解鎖原簽核人的日子 → 200。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "B 解 A 的：發現少收一筆", "is_admin_override": False},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["close_date"] == target.isoformat()
    assert body["is_admin_override"] is False
    # 確認 ApprovalLog 寫入
    with sf() as s:
        log = (
            s.query(ApprovalLog)
            .filter(ApprovalLog.doc_type == "activity_pos_daily")
            .order_by(ApprovalLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.action == "cancelled"
        assert log.approver_username == "approver_b"


def test_admin_override_with_long_reason_succeeds(unlock_client):
    """role='admin' + override + reason ≥ 30 字 → 200，ApprovalLog action='admin_override'。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        # admin role + PAYMENT_APPROVE
        _create_admin(s, username="boss", permissions=APPROVE_PERMS)
        # role 預設 'admin' from _create_admin
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="boss")

    assert _login(client, "boss").status_code == 200
    long_reason = "副手請假，緊急 override 解鎖修正昨日帳務漏記 NT$500 部分"  # ≥ 30 字
    assert len(long_reason) >= 30
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": long_reason, "is_admin_override": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["is_admin_override"] is True
    with sf() as s:
        log = (
            s.query(ApprovalLog)
            .filter(ApprovalLog.doc_type == "activity_pos_daily")
            .order_by(ApprovalLog.id.desc())
            .first()
        )
        assert log.action == "admin_override"
        assert log.approver_role == "admin"


def test_admin_override_short_reason_rejected_422(unlock_client):
    """admin override 但 reason < 30 字 → 422（schema 層 model_validator）。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="boss", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="boss")

    assert _login(client, "boss").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "太短不夠 30 字測試案例", "is_admin_override": True},
    )
    assert res.status_code == 422, res.text
    assert "30" in res.text


def test_non_admin_with_override_flag_rejected_403(unlock_client):
    """非 admin role 帶 is_admin_override=True → 403。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        # role='staff' 而非 'admin'
        _create_admin(s, username="staff_x", role="staff", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "staff_x").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "假裝自己是 admin 嘗試 override 但其實沒 admin role 的測試",
            "is_admin_override": True,
        },
    )
    assert res.status_code == 403, res.text
    assert "admin" in res.json()["detail"].lower() or "admin" in res.text
```

> **注意**：`_create_admin` 預設 role='admin'。Test 5 需明確傳 `role="staff"`。需先檢查 `_create_admin` 是否支援 `role` kwarg；若不支援，plan 階段 inline create User 直寫 DB。

### - [ ] Step 2: 跑測試確認 FAIL

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py -v 2>&1 | tail -40
```

**預期**：5 個測試裡，至少 test 1, 3, 4, 5 FAIL（schema 還沒 `is_admin_override`、handler 還沒 4-eye 守衛）。

如果 fixture import 階段 ERROR（例如 `_create_admin` 不接受 `role` kwarg），先檢查 `tests/test_activity_pos.py:_create_admin` 簽章；若確實不支援，把 Test 5 fixture 改為直接 `s.add(User(...))` 寫 DB（用 `from models.database import User` 或 `models.auth import User`）。

### - [ ] Step 3: 實作 schema 收口

修改 `/Users/yilunwu/Desktop/ivy-backend/api/activity/pos_approval.py`：

**(a)** 在現有 `_UNLOCK_REASON_MIN_LENGTH = 10` 上方加新常數（位置：約 L55）：

```python
_UNLOCK_REASON_MIN_LENGTH = 10
_ADMIN_OVERRIDE_REASON_MIN_LENGTH = 30
```

**(b)** 替換 `class DailyCloseUnlock(BaseModel)` 整段（位置：約 L58-83）為：

```python
class DailyCloseUnlock(BaseModel):
    """解鎖日結簽核的請求。

    一般 4-eye 路徑：reason ≥ 10 字 + 解鎖人 ≠ 原簽核人（handler 守衛）。
    Admin override 路徑：is_admin_override=True + reason ≥ 30 字 + role='admin'（handler 守衛）。

    Why: 原設計只擋 reason 長度，未限制「自簽自解」循環；spec C2 收緊。
    """

    reason: str = Field(..., max_length=500)
    is_admin_override: bool = Field(
        False,
        description=(
            "管理員緊急 override：略過 4-eye 但 reason 須 ≥ "
            f"{_ADMIN_OVERRIDE_REASON_MIN_LENGTH} 字"
        ),
    )

    @model_validator(mode="after")
    def _validate_reason_length(self):
        cleaned = (self.reason or "").strip()
        min_len = (
            _ADMIN_OVERRIDE_REASON_MIN_LENGTH
            if self.is_admin_override
            else _UNLOCK_REASON_MIN_LENGTH
        )
        if len(cleaned) < min_len:
            extra = (
                "（admin override 須具體說明緊急情況）"
                if self.is_admin_override
                else ""
            )
            raise ValueError(f"解鎖原因需至少 {min_len} 字{extra}")
        self.reason = cleaned
        return self
```

> **import**：確保 `model_validator` 已 import；若沒，把 `from pydantic import BaseModel, Field` 行加上 `, model_validator`，並把既有 `field_validator` 一同保留。

### - [ ] Step 4: 實作 handler 4-eye 守衛

在 `unlock_daily_close` handler（位置：約 L352）內，**取得 row 後、執行 delete 前**插入守衛。

找到既有區塊：
```python
        row = (
            session.query(ActivityPosDailyClose)
            .filter(ActivityPosDailyClose.close_date == target)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="該日尚未簽核，無需解鎖")

        original_approver = row.approver_username
```

緊接 `original_approver = row.approver_username` 後加：

```python
        # ── 4-eye 守衛 ──────────────────────────────────────
        # Admin override 路徑：必須 role='admin'（不論是否原簽核人）
        # 一般路徑：解鎖人 ≠ 原簽核人
        # Why: 同一人簽 → 解 → 改 → 重簽循環會無痕修帳；強制分離以保稽核獨立性
        if body.is_admin_override:
            if current_user.get("role") != "admin":
                raise HTTPException(
                    status_code=403,
                    detail="僅 admin 角色可進行 override 解鎖；請改用一般 4-eye 流程",
                )
        elif current_user.get("username") == original_approver:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"解鎖人不可為原簽核人 {original_approver}；"
                    "請由其他簽核權限者執行，或以 admin 身分 override"
                ),
            )
```

### - [ ] Step 5: ApprovalLog action 區分 + 改 response

在同一 handler 內，找到既有寫入 `ApprovalLog` 的區塊（約 L420）：

```python
        session.delete(row)
        session.add(
            ApprovalLog(
                doc_type="activity_pos_daily",
                doc_id=_doc_id_for(target),
                action="cancelled",
                approver_username=current_user.get("username", ""),
                approver_role=current_user.get("role"),
                comment=comment,
            )
        )
```

把 `action="cancelled"` 改為動態：

```python
        action_value = "admin_override" if body.is_admin_override else "cancelled"
        session.delete(row)
        session.add(
            ApprovalLog(
                doc_type="activity_pos_daily",
                doc_id=_doc_id_for(target),
                action=action_value,
                approver_username=current_user.get("username", ""),
                approver_role=current_user.get("role"),
                comment=comment,
            )
        )
```

接著 `session.commit()` 後，把既有的 `return None`（204 path）替換為 200 + JSON：

找到：
```python
        request.state.audit_changes = {
            ...
            "reason": body.reason,
        }
        return None
    except HTTPException:
        ...
```

把 `return None` 替換為（注意：通知部分留待 Task 2，這裡先給 placeholder False）：

```python
        request.state.audit_changes = {
            "close_date": target.isoformat(),
            "original_approver": original_approver,
            "original_approved_at": original_at,
            "original_payment_total": original_payment,
            "original_refund_total": original_refund,
            "original_net_total": original_net,
            "original_transaction_count": original_tx,
            "reason": body.reason,
            "is_admin_override": body.is_admin_override,
        }
        # Task 2 會在此插入 LINE 通知；先給預設 False
        notification_delivered = False
        return {
            "close_date": target.isoformat(),
            "unlocked_at": datetime.now().isoformat(timespec="seconds"),
            "is_admin_override": body.is_admin_override,
            "notification_delivered": notification_delivered,
        }
```

並把 router decorator 的 `status_code=status.HTTP_204_NO_CONTENT` 改為 `status_code=200`：

```python
@router.delete("/pos/daily-close/{date_str}", status_code=200)
```

### - [ ] Step 6: 跑測試確認 PASS

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py -v 2>&1 | tail -20
```

**預期**：Test 1-5 全 PASS。

如果 Test 2 (`test_unlock_by_other_approver_succeeds`) 在斷言 `body["close_date"]` 失敗（因為先前 204 沒 body），代表 router status_code 改動成功，但測試需要的 200 已就位。

### - [ ] Step 7: 不 commit（與 Task 5 合併）

Plan §5 一次提交所有後端變更。

---

## Task 2: LINE 通知方法 + best-effort

**Repo:** `ivy-backend`

**Files:**
- Modify: `services/line_service.py`（新增 `notify_pos_unlock_to_approver` 方法）
- Modify: `api/activity/pos_approval.py`（call 通知 + best-effort try/except）
- Modify: `tests/test_pos_unlock_separation.py`（追加 Test 6）

**Spec ref:** §0 成功標準 6、§1.3

### - [ ] Step 1: 寫失敗測試

在 `tests/test_pos_unlock_separation.py` 末尾追加：

```python
# ── Test 6: notification_delivered ────────────────────────────────────


def test_unlock_response_notification_delivered_false_when_no_line_binding(
    unlock_client,
):
    """原簽核人未綁定 LINE → response notification_delivered=false，unlock 仍成功。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        # 兩人預設都未綁 line_user_id
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "B 解 A 的；A 未綁 LINE 測試", "is_admin_override": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["notification_delivered"] is False
```

### - [ ] Step 2: 跑測試確認 PASS（已 PASS — 因為 Task 1 將通知設為 False placeholder）

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py::test_unlock_response_notification_delivered_false_when_no_line_binding -v
```

**預期**：PASS（因 Task 1 預設 False）。這是「保護性」測試，等 Task 2 實作通知後仍會 PASS。

### - [ ] Step 3: 在 line_service.py 新增方法

打開 `/Users/yilunwu/Desktop/ivy-backend/services/line_service.py`，在 class 內找到合適位置（建議：`_push_to_user` method 下方，約 L274 之後）。新增：

```python
    def notify_pos_unlock_to_approver(
        self,
        *,
        target_date,
        original_approver: str,
        unlocker: str,
        is_override: bool,
        reason: str,
    ) -> bool:
        """通知原簽核人：他簽過的日結被解鎖。

        Returns: True 若推播成功送出；False 若無 LINE 綁定或推播失敗。
        Best-effort：呼叫端不應因 False 中止 unlock 流程。

        Why (spec C2): 解鎖事件需即時告知原簽核人以強化稽核獨立性；
        員工端 LINE 綁定屬另案，未綁則 silent fail，由 dashboard 補救。
        """
        if not self._enabled or not self._token:
            return False
        from models.base import _SessionFactory
        from models.auth import User

        if _SessionFactory is None:
            return False
        session = _SessionFactory()
        try:
            user = (
                session.query(User)
                .filter(
                    User.username == original_approver,
                    User.is_active.is_(True),
                )
                .first()
            )
            if not user or not user.line_user_id or not user.line_follow_confirmed_at:
                return False

            label = "管理員 override 解鎖" if is_override else "解鎖"
            msg = (
                f"📝 POS 日結{label}通知\n"
                f"日期：{target_date.isoformat()}\n"
                f"原簽核人：{original_approver}（您）\n"
                f"解鎖人：{unlocker}\n"
                f"原因：{reason}\n\n"
                "請至後台確認異常稽核軌跡。"
            )
            return self._push_to_user(user.line_user_id, msg)
        finally:
            session.close()
```

### - [ ] Step 4: 在 unlock handler 串接通知

回到 `api/activity/pos_approval.py`，把 Task 1 Step 5 留下的 placeholder：

```python
        # Task 2 會在此插入 LINE 通知；先給預設 False
        notification_delivered = False
```

替換為：

```python
        # ── LINE 通知（best-effort；失敗不擋已 commit 的解鎖）─────────
        # Why: 原簽核人需即時知悉自己簽過的日子被解鎖；無綁定則 silent，
        # response.notification_delivered=false 提示解鎖人私下告知對方。
        notification_delivered = False
        try:
            from services.line_service import line_service
            notification_delivered = line_service.notify_pos_unlock_to_approver(
                target_date=target,
                original_approver=original_approver,
                unlocker=current_user.get("username", ""),
                is_override=body.is_admin_override,
                reason=body.reason,
            )
        except Exception:
            logger.warning("LINE notify on POS unlock failed", exc_info=True)
```

> **import 確認**：`line_service` singleton 通常已在 services 模組頂層 export；plan 實作時檢查 `services/line_service.py` 末尾是否有 `line_service = LineNotificationService()` 之類；若沒，改用 `services.line_service.get_line_service()` 或同等 helper。實作時看 line_service.py 模組結構。

### - [ ] Step 5: 跑測試確認 PASS

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py -v
```

**預期**：6 個測試全 PASS。

### - [ ] Step 6: 不 commit

---

## Task 3: approve warnings

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos_approval.py`（approve_daily_close handler）
- Modify: `tests/test_pos_unlock_separation.py`（追加 Test 7-8）

**Spec ref:** §0 成功標準 7、§1.2

### - [ ] Step 1: 寫失敗測試

在 `tests/test_pos_unlock_separation.py` 末尾追加：

```python
# ── Test 7-8: approve warnings ────────────────────────────────────────


def test_approve_warnings_when_approver_is_today_operator(unlock_client):
    """簽核者 = 當日 POS 操作者 → response 帶 warnings 提示。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        reg = _make_reg_minimal(s, student_name="X")
        # 當日由 approver_a 操作的一筆 payment（_add_payment 預設 operator='admin'）
        # 所以這裡需明確指定 operator
        _add_payment(
            s,
            reg.id,
            type_="payment",
            amount=500,
            method="現金",
            day=target,
            operator="approver_a",
        )
        s.commit()

    assert _login(client, "approver_a").status_code == 200
    res = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"note": "approver = today operator"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert "warnings" in body
    assert any("收銀者" in w for w in body["warnings"])


def test_approve_no_warnings_when_approver_did_not_operate_today(unlock_client):
    """簽核者 ≠ 當日 POS 操作者 → warnings 為空陣列。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        reg = _make_reg_minimal(s, student_name="Y")
        _add_payment(
            s,
            reg.id,
            type_="payment",
            amount=500,
            method="現金",
            day=target,
            operator="other_cashier",  # 非簽核人
        )
        s.commit()

    assert _login(client, "approver_a").status_code == 200
    res = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"note": "approver != operator"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body.get("warnings", []) == []
```

> **注意**：`_add_payment` helper 是否支援 `operator` kwarg？檢查 `tests/test_activity_pos.py:1019` 區域。若不支援，把 fixture 改為直接 `s.add(ActivityPaymentRecord(...))`。

### - [ ] Step 2: 跑測試確認 FAIL

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py::test_approve_warnings_when_approver_is_today_operator tests/test_pos_unlock_separation.py::test_approve_no_warnings_when_approver_did_not_operate_today -v
```

**預期**：兩個 FAIL（response 沒 `warnings` key）。

### - [ ] Step 3: 實作 approve warnings

打開 `/Users/yilunwu/Desktop/ivy-backend/api/activity/pos_approval.py`，找到 `approve_daily_close` handler。

定位 `session.refresh(row)` 後、`return _serialize_close(row)` 前的位置（約 L320-340 之間）。

在 `session.refresh(row)` 之後插入 warnings 計算 + 改 return：

```python
        session.refresh(row)
        # ── 軟提醒：簽核者 = 當日 POS 操作者 ─────────────────────
        # Why (spec C2): 當日收銀者自簽會降低稽核獨立性；不擋送出，僅提示
        operators_today = {
            op
            for (op,) in session.query(ActivityPaymentRecord.operator)
            .filter(
                ActivityPaymentRecord.payment_date == target,
                ActivityPaymentRecord.voided_at.is_(None),
            )
            .distinct()
            .all()
            if op
        }
        warnings: list[str] = []
        approver_name = current_user.get("username", "")
        if approver_name and approver_name in operators_today:
            warnings.append(
                f"你（{approver_name}）是當日 POS 收銀者；"
                "建議由其他簽核者覆核以強化稽核獨立性"
            )

        logger.warning(
            "POS 日結簽核：date=%s approver=%s net=%d variance=%s warnings=%d",
            target.isoformat(),
            approver_name,
            snap["net"],
            cash_variance,
            len(warnings),
        )
        # （audit_* 區塊保留原樣，跳過）
        ...
        response = _serialize_close(row)
        response["warnings"] = warnings
        return response
```

> **位置精確化**：實作時請完整保留 `request.state.audit_*` 區塊（既有），把最後的 `return _serialize_close(row)` 替換為兩行 `response = _serialize_close(row); response["warnings"] = warnings; return response`。

> **import**：`ActivityPaymentRecord` 已在 file 頂端 import。

### - [ ] Step 4: 跑測試確認 PASS

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py -v
```

**預期**：8 個測試全 PASS。

### - [ ] Step 5: 不 commit

---

## Task 4: audit endpoint

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos_approval.py`（新 endpoint + helper）
- Modify: `tests/test_pos_unlock_separation.py`（追加 Test 9-10）

**Spec ref:** §0 成功標準 8、§1.4

### - [ ] Step 1: 寫失敗測試

在 `tests/test_pos_unlock_separation.py` 末尾追加：

```python
# ── Test 9-10: audit endpoint ────────────────────────────────────────


def test_audit_endpoint_returns_recent_unlock_events_only(unlock_client):
    """audit endpoint 只回傳 doc_type='activity_pos_daily' + action 在 unlock 集合的事件。"""
    client, sf = unlock_client
    target_a = date.today() - timedelta(days=1)
    target_b = date.today() - timedelta(days=2)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target_a, approver_username="approver_a")
    _seed_signed_close(sf, target=target_b, approver_username="approver_a")

    # B 解 A 簽的 target_a → cancelled 事件
    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target_a.isoformat()}",
        json={"reason": "B 解 A 的測試 cancelled 事件 audit", "is_admin_override": False},
    )
    assert res.status_code == 200

    # 寫入一筆無關 doc_type 的 ApprovalLog 確認過濾正確
    from models.database import ApprovalLog as _AL
    with sf() as s:
        s.add(_AL(
            doc_type="leave",
            doc_id=999,
            action="approved",
            approver_username="approver_b",
        ))
        s.commit()

    # 查 audit endpoint
    res = client.get("/api/activity/audit/pos-unlock-events?days=30")
    assert res.status_code == 200, res.text
    body = res.json()
    events = body["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["close_date"] == target_a.isoformat()
    assert ev["action"] == "cancelled"
    assert ev["unlocker_username"] == "approver_b"


def test_audit_endpoint_orders_desc_and_limits_200(unlock_client):
    """構造 250 筆 unlock 事件，回傳 200 筆按時間倒序。"""
    client, sf = unlock_client
    from datetime import datetime
    from models.database import ApprovalLog as _AL
    with sf() as s:
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        # 構造 250 筆 cancelled 事件
        base = datetime.now()
        for i in range(250):
            s.add(_AL(
                doc_type="activity_pos_daily",
                doc_id=20260101 + i,
                action="cancelled",
                approver_username="approver_b",
                created_at=base - timedelta(seconds=i),
            ))
        s.commit()

    assert _login(client, "approver_b").status_code == 200
    res = client.get("/api/activity/audit/pos-unlock-events?days=30")
    assert res.status_code == 200
    events = res.json()["events"]
    assert len(events) == 200  # limit
    # 倒序：第一筆 occurred_at 應大於最後一筆
    assert events[0]["occurred_at"] > events[-1]["occurred_at"]
```

### - [ ] Step 2: 跑測試確認 FAIL

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py::test_audit_endpoint_returns_recent_unlock_events_only tests/test_pos_unlock_separation.py::test_audit_endpoint_orders_desc_and_limits_200 -v
```

**預期**：兩個 FAIL（endpoint 不存在 → 404）。

### - [ ] Step 3: 實作 endpoint

在 `/Users/yilunwu/Desktop/ivy-backend/api/activity/pos_approval.py` 末尾加：

```python
# ── 端點 6：解鎖事件儀表板（spec C2）────────────────────


def _doc_id_to_date(doc_id: int):
    """將 ApprovalLog.doc_id (YYYYMMDD int) 解回 date；解析失敗回 None。"""
    s = str(doc_id)
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


@router.get("/activity/audit/pos-unlock-events")
async def list_pos_unlock_events(
    days: int = Query(30, ge=1, le=180, description="查詢過去 N 天"),
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """列出近 N 天的 POS 日結解鎖事件（一般 4-eye + admin override）。

    時間倒序，限 200 筆；ApprovalLog 為 source of truth。
    供老闆/簽核者隨時查看異常解鎖記錄，補強稽核獨立性。
    """
    cutoff = datetime.now(TAIPEI_TZ).replace(tzinfo=None) - timedelta(days=days)
    session = get_session()
    try:
        rows = (
            session.query(ApprovalLog)
            .filter(
                ApprovalLog.doc_type == "activity_pos_daily",
                ApprovalLog.action.in_(["cancelled", "admin_override"]),
                ApprovalLog.created_at >= cutoff,
            )
            .order_by(ApprovalLog.created_at.desc())
            .limit(200)
            .all()
        )
        events = []
        for r in rows:
            close_dt = _doc_id_to_date(r.doc_id)
            events.append({
                "id": r.id,
                "close_date": close_dt.isoformat() if close_dt else None,
                "action": r.action,
                "unlocker_username": r.approver_username,
                "unlocker_role": r.approver_role,
                "comment": r.comment,
                "occurred_at": (
                    r.created_at.isoformat(timespec="seconds")
                    if r.created_at else None
                ),
            })
        return {
            "days": days,
            "count": len(events),
            "events": events,
        }
    finally:
        session.close()
```

> **注意 router prefix**：`api/activity/__init__.py` 通常會把 router prefix 設為 `/activity` 或 `/api/activity`。這個新 endpoint 路徑是 `/activity/audit/pos-unlock-events`，但若 prefix 已是 `/activity`，則 decorator 應寫 `@router.get("/audit/pos-unlock-events")`。

> **action plan**：先檢查 `api/activity/__init__.py` 看 router prefix；若 prefix=`/activity`，把 decorator 改為 `"/audit/pos-unlock-events"`；若 prefix=`""` 或不同，則保持 `"/activity/audit/pos-unlock-events"`。Test fixture 用的 path 為 `/api/activity/audit/pos-unlock-events`（main app `/api` prefix + activity router prefix `/activity`）— 這是真實線上 URL，與 fixture 對齊即可。

### - [ ] Step 4: 跑測試確認 PASS

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_unlock_separation.py -v
```

**預期**：10 個測試全 PASS。

如果 fixture endpoint URL 不對（404），檢查 router prefix 後調整 decorator 或測試 URL。

### - [ ] Step 5: 不 commit

---

## Task 5: 修舊測試 + 後端 final commit

**Repo:** `ivy-backend`

**Files:**
- Modify: `tests/test_activity_pos.py`（修舊解鎖測試）

### - [ ] Step 1: 跑舊測試找紅燈

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_pos.py -v 2>&1 | grep -E "FAILED|ERROR" | head -20
```

**預期紅燈類型**：
- 既有 unlock 測試以 `_create_admin` 預設 username='pos_admin' 簽核 + 同一 client 解鎖 → 觸發新 4-eye 守衛 → 403
- 既有 unlock 測試的 reason 字數可能 < 30（admin override path 下不適用）

### - [ ] Step 2: 修舊 unlock 測試

對於 `tests/test_activity_pos.py::TestPosDailyClose::test_unlock_*` 系列，**有兩種修法**：

**方法 A**：建立第二個 admin 帳號做解鎖（保留 4-eye 語意）

例如把：
```python
def test_unlock_deletes_row_and_writes_cancel_log(self, pos_client):
    client, sf = pos_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, permissions=self.APPROVE_PERMS)
        reg = _make_reg_minimal(s, student_name="X")
        _add_payment(s, reg.id, type_="payment", amount=100, method="現金", day=target)
        s.commit()
    assert _login(client).status_code == 200
    # 先簽
    client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
    # 再解
    res = client.request("DELETE", f"/api/activity/pos/daily-close/{target.isoformat()}",
                          json={"reason": "test unlock reason"})
    ...
```

改為：
```python
def test_unlock_deletes_row_and_writes_cancel_log(self, pos_client):
    client, sf = pos_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=self.APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=self.APPROVE_PERMS)
        reg = _make_reg_minimal(s, student_name="X")
        _add_payment(s, reg.id, type_="payment", amount=100, method="現金", day=target)
        s.commit()
    # A 簽
    assert _login(client, "approver_a").status_code == 200
    client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
    # B 解（4-eye）
    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "test unlock reason long enough"},
    )
    ...
```

**方法 B**：改為 admin override 路徑（保留同一帳號）

```python
res = client.request(
    "DELETE",
    f"/api/activity/pos/daily-close/{target.isoformat()}",
    json={
        "reason": "原簽核人 admin override 解鎖測試案例（≥ 30 字）",
        "is_admin_override": True,
    },
)
```

> **選擇原則**：原測試斷言 `action="cancelled"` → 用方法 A（保持 cancelled 語義）。原測試斷言 `comment` 內容 → 兩者都可。

實作時逐一檢查紅燈測試並用對應方法修復。

> **特殊**：`test_unlock_without_reason_rejected_422` 既有測試應仍 PASS（reason 缺失，schema 層直接 422，不到 4-eye）。

### - [ ] Step 3: 跑全 backend 測試確認綠

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_pos.py tests/test_pos_unlock_separation.py tests/test_pos_cash_only.py 2>&1 | tail -5
```

**預期**：all green。

### - [ ] Step 4: 後端 commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/activity/pos_approval.py services/line_service.py \
        tests/test_pos_unlock_separation.py tests/test_activity_pos.py \
        docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md \
        docs/superpowers/plans/2026-05-06-pos-unlock-separation.md
git status
git commit -m "$(cat <<'EOF'
feat(activity-pos): POS 日結解鎖 4-eye + admin override + 異常稽核 dashboard

POS 日結原本的「自簽自解」漏洞：同一人可循環簽 → 解 → 改 → 重簽
無痕修帳。本次強制 4-eye 並加上監督機制（spec C2）。

- DELETE /pos/daily-close/{date}：
  * 一般路徑：解鎖人 ≠ 原簽核人，否則 403
  * admin override：role='admin' + is_admin_override=true + reason ≥ 30 字
  * Response 從 204 改為 200 + JSON（is_admin_override, notification_delivered）
- POST /pos/daily-close/{date}：
  * 簽核者 = 當日 POS 操作者時 response 帶 warnings（軟提醒，不擋）
- 新增 GET /activity/audit/pos-unlock-events：
  * 近 N 天解鎖事件（含 admin override），時間倒序，限 200 筆
- 新增 line_service.notify_pos_unlock_to_approver（best-effort）：
  * 推 LINE 給原簽核人；無綁定/失敗則 silent，不擋 commit
- ApprovalLog action 區分：cancelled（一般）/ admin_override（緊急）
- 新增 tests/test_pos_unlock_separation.py（10 個測試）
- 修舊 unlock 測試：分離 approver_a / approver_b 帳號以符合 4-eye

對應文件：
- docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md
- docs/superpowers/plans/2026-05-06-pos-unlock-separation.md

API breaking change：DELETE /pos/daily-close/{date} 從 204 改為 200+JSON。
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git log -1 --stat | head -10
```

---

## Task 6: 前端 unlock 三分支 UI

**Repo:** `ivy-frontend`

**Files:**
- Modify: `src/api/activity.js`（unlockPOSDailyClose 用 data 帶 payload；新增 getPOSUnlockEvents）
- Modify: `src/views/activity/POSApprovalView.vue`（handleUnlock 重構為三分支）

**Spec ref:** §2.1, §2.2

### - [ ] Step 1: 修改 src/api/activity.js

打開 `/Users/yilunwu/Desktop/ivy-frontend/src/api/activity.js`，找到既有 `unlockPOSDailyClose`（之前 `data: { reason }`），確認其結構。

把它換成（payload 改帶 `is_admin_override`）：

```javascript
export const unlockPOSDailyClose = (date, payload) =>
  // payload: { reason: string, is_admin_override?: boolean }
  // 後端 response: 200 + { close_date, unlocked_at, is_admin_override, notification_delivered }
  api.delete(`/activity/pos/daily-close/${date}`, { data: payload })
```

> 注意：既有呼叫處可能傳 `data: { reason }`；新版接受 `is_admin_override` 為 optional false。後續 Task 6 Step 4 會更新呼叫處。

在文件末尾（或 POS 相關 API 群組末）加新 API：

```javascript
export const getPOSUnlockEvents = (days = 30) =>
  api.get('/activity/audit/pos-unlock-events', { params: { days } })
```

### - [ ] Step 2: 修改 POSApprovalView handleUnlock

打開 `/Users/yilunwu/Desktop/ivy-frontend/src/views/activity/POSApprovalView.vue`。

**(a)** 在 `<script setup>` 區塊找到既有 `import { hasPermission } from '@/utils/auth'`，補 `getUserInfo`：

```javascript
import { getUserInfo, hasPermission } from '@/utils/auth'
```

**(b)** 找到既有 `handleUnlock` function（既有大約在 L538-580 區，依當前 file 為準），整段替換為：

```javascript
async function handleUnlock() {
  if (!canApprove.value) return

  const userInfo = getUserInfo()
  const myUsername = userInfo?.username || ''
  const myRole = userInfo?.role || ''
  const originalApprover = detail.value?.approver_username || ''

  const isOriginal = myUsername && myUsername === originalApprover
  const isAdmin = myRole === 'admin'

  // 分支 1：非原簽核人 → 一般 4-eye 路徑
  if (!isOriginal) {
    return doUnlock({ isOverride: false, minLen: 10 })
  }

  // 分支 2：原簽核人但非 admin → 擋下並提示
  if (!isAdmin) {
    ElMessageBox.alert(
      `您是原簽核人 ${originalApprover}；解鎖必須由其他簽核者執行。\n\n` +
        '若情況緊急且具備管理員身分，請聯繫系統管理員協助 override。',
      '無法解鎖',
      { type: 'warning', confirmButtonText: '了解' }
    ).catch(() => {})
    return
  }

  // 分支 3：原簽核人 + admin → override 路徑（雙確認 + 30 字 reason）
  try {
    await ElMessageBox.confirm(
      '⚠️ 您是原簽核人；以管理員身分 override 解鎖會寫入特殊稽核紀錄並 LINE 通知您自己（測試）。\n\n' +
        '建議優先請其他簽核者解鎖；override 應僅用於對方不在的緊急情況。',
      'Admin Override 解鎖',
      {
        confirmButtonText: '我了解，繼續 override',
        cancelButtonText: '取消',
        type: 'warning',
      }
    )
  } catch {
    return // 使用者取消
  }

  return doUnlock({ isOverride: true, minLen: 30 })
}

async function doUnlock({ isOverride, minLen }) {
  let reason
  try {
    const res = await ElMessageBox.prompt(
      `請輸入解鎖原因（≥ ${minLen} 字）：`,
      isOverride ? 'Override 原因' : '解鎖原因',
      {
        inputType: 'textarea',
        confirmButtonText: '確認解鎖',
        cancelButtonText: '取消',
        inputValidator: (v) =>
          (v || '').trim().length >= minLen || `至少 ${minLen} 字`,
      }
    )
    reason = (res.value || '').trim()
  } catch {
    return // 使用者取消
  }

  submitting.value = true
  try {
    const { data } = await unlockPOSDailyClose(selectedDate.value, {
      reason,
      is_admin_override: isOverride,
    })
    ElMessage.success(isOverride ? '已 override 解鎖；通知已發送' : '已解鎖')
    if (data && data.notification_delivered === false) {
      ElMessage.warning(
        '原簽核人未綁定 LINE，未收到自動通知；請私下告知對方。',
        { duration: 6000 }
      )
    }
    await refreshAll()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '解鎖失敗')
  } finally {
    submitting.value = false
  }
}
```

> **既有 handleUnlock 內可能有 `FIELD_RULES.unlockReasonMin`** 之類常數；確認後若有，將該常數改為動態（10 / 30），或直接用 minLen。

### - [ ] Step 3: 視覺驗證

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
# 開瀏覽器 http://localhost:5173/activity/pos/approval
```

**手動驗證流程**（需要 admin/admin123 + 另一個有 PAYMENT_APPROVE 權限的帳號）：
- 用 A 簽核某天 → 用 A 嘗試解鎖 → 看到 ElMessageBox.alert 擋下
- 切 admin 帳號（若 admin/admin123 有 PAYMENT_APPROVE）→ 嘗試解鎖 → 出現 override 雙確認 + 30 字 prompt
- 用 B（不同 PAYMENT_APPROVE 持有者）→ 嘗試解鎖 → 一般 prompt（10 字）

> 若 dev DB 沒第二個 PAYMENT_APPROVE 帳號，整合驗證在 Task 10 全面做；本 Step 至少確認分支 1（非原簽核人路徑）流程順暢。

### - [ ] Step 4: 不 commit

---

## Task 7: 前端 approve warnings + audit 入口連結

**Repo:** `ivy-frontend`

**Files:**
- Modify: `src/views/activity/POSApprovalView.vue`（handleApprove 顯示 warnings、新增「異常稽核軌跡」按鈕）

### - [ ] Step 1: handleApprove 顯示 warnings

在 `POSApprovalView.vue` 找到 `handleApprove` function。在 `await approvePOSDailyClose(...)` 那行附近，把：

```javascript
    await approvePOSDailyClose(selectedDate.value, {
      note: form.note || null,
      actual_cash_count: cash == null ? null : Number(cash),
    })
    ElMessage.success('簽核完成')
```

改為（接收 response 並逐條顯示 warnings）：

```javascript
    const { data } = await approvePOSDailyClose(selectedDate.value, {
      note: form.note || null,
      actual_cash_count: cash == null ? null : Number(cash),
    })
    const warnings = (data && data.warnings) || []
    warnings.forEach((w) => {
      ElMessage.warning({ message: w, duration: 6000, showClose: true })
    })
    ElMessage.success('簽核完成')
```

### - [ ] Step 2: 加「異常稽核軌跡」入口按鈕

在 `POSApprovalView.vue` template 中找到頁面 head 區塊（含日期選擇 / 切換等），加一個 router-link 按鈕。

例如在現有 `<el-card>` 標題附近：

```vue
<el-button
  v-if="canApprove"
  size="small"
  :icon="Warning"
  @click="$router.push('/activity/audit/pos-unlock')"
>
  異常稽核軌跡
</el-button>
```

> import 補上：`import { Warning } from '@element-plus/icons-vue'`；確認原本 import 區塊有此 vendor 否則加上。

### - [ ] Step 3: 視覺驗證

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

簽核某天時：
- 若簽核者 = 當日操作者 → 看到橘色 ElMessage 警告（不擋送出）
- 簽核頁有「異常稽核軌跡」按鈕（點擊後 Task 8 才有目標 view）

### - [ ] Step 4: 不 commit

---

## Task 8: 新 view POSAuditEventsView + 路由

**Repo:** `ivy-frontend`

**Files:**
- Create: `src/views/activity/POSAuditEventsView.vue`
- Modify: `src/router/index.js`（加 route）

### - [ ] Step 1: 建立 POSAuditEventsView.vue

建立 `/Users/yilunwu/Desktop/ivy-frontend/src/views/activity/POSAuditEventsView.vue`：

```vue
<template>
  <el-card class="pos-audit">
    <template #header>
      <div class="pos-audit__head">
        <h2 class="pos-audit__title">POS 日結異常稽核軌跡</h2>
        <el-select
          v-model="days"
          size="small"
          style="width: 120px"
          @change="load"
        >
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
          <el-option :value="180" label="近 180 天" />
        </el-select>
      </div>
    </template>

    <el-empty
      v-if="!loading && events.length === 0"
      :description="`近 ${days} 天無解鎖事件`"
      :image-size="80"
    />

    <el-timeline v-else>
      <el-timeline-item
        v-for="ev in events"
        :key="ev.id"
        :timestamp="ev.occurred_at"
        :type="ev.action === 'admin_override' ? 'danger' : 'warning'"
        placement="top"
      >
        <div class="pos-audit__event">
          <strong class="pos-audit__event-title">
            {{ ev.action === 'admin_override' ? '🔓 Admin Override 解鎖' : '🔓 解鎖' }}
            — {{ ev.close_date || '—' }}
          </strong>
          <div class="pos-audit__event-meta">
            解鎖人：<code>{{ ev.unlocker_username }}</code>
            <span v-if="ev.unlocker_role">（{{ ev.unlocker_role }}）</span>
          </div>
          <div class="pos-audit__event-comment">{{ ev.comment }}</div>
        </div>
      </el-timeline-item>
    </el-timeline>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'

import { getPOSUnlockEvents } from '@/api/activity'

const days = ref(30)
const events = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const { data } = await getPOSUnlockEvents(days.value)
    events.value = data.events || []
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '載入失敗')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pos-audit__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.pos-audit__title {
  margin: 0;
  font-size: 18px;
}

.pos-audit__event-title {
  display: block;
  margin-bottom: 4px;
}

.pos-audit__event-meta {
  font-size: 13px;
  color: #64748b;
  margin-bottom: 4px;
}

.pos-audit__event-comment {
  font-size: 13px;
  color: #475569;
  white-space: pre-wrap;
  background: #f8fafc;
  padding: 8px 10px;
  border-radius: 6px;
}
</style>
```

### - [ ] Step 2: 加路由

打開 `/Users/yilunwu/Desktop/ivy-frontend/src/router/index.js`。

找到既有 activity 相關路由（搜尋 `/activity/pos` 之類），在同區附近加：

```javascript
{
  path: '/activity/audit/pos-unlock',
  name: 'POSAuditEvents',
  component: () => import('@/views/activity/POSAuditEventsView.vue'),
  meta: {
    requiresAuth: true,
    requiresPermission: 'ACTIVITY_PAYMENT_APPROVE',
  },
},
```

> 路由格式因 router 設定而異；plan 實作時 grep 既有 route 寫法 (`grep -n "/activity/pos" src/router/index.js`) 並對齊。

### - [ ] Step 3: 整合驗證

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

從 POSApprovalView 點「異常稽核軌跡」 → 跳到新 view → 預期空清單（dev 環境通常無資料）或顯示既有 ApprovalLog 解鎖紀錄。

### - [ ] Step 4: 不 commit（等 Task 9）

---

## Task 9: 前端 final commit

**Repo:** `ivy-frontend`

### - [ ] Step 1: 跑前端 test + build

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm test 2>&1 | tail -5
npm run build 2>&1 | tail -5
```

**預期**：test 全綠（無 POS 相關 unit test 被影響）；build 成功。

### - [ ] Step 2: commit

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/api/activity.js src/views/activity/POSApprovalView.vue \
        src/views/activity/POSAuditEventsView.vue src/router/index.js
git status
git commit -m "$(cat <<'EOF'
feat(activity-pos): POS 日結解鎖 4-eye UI + 異常稽核 dashboard

對齊後端 spec C2 4-eye 強制（後端 commit 同步）。

- POSApprovalView.handleUnlock 三分支：
  * 非原簽核人 → 一般 4-eye prompt（≥ 10 字）
  * 原簽核人非 admin → ElMessageBox.alert 擋下提示
  * 原簽核人 admin → override 雙確認 + 30 字 prompt
- POSApprovalView.handleApprove 顯示 backend warnings
- POSApprovalView 加「異常稽核軌跡」入口按鈕
- 新增 POSAuditEventsView：el-timeline 顯示近 7/30/90/180 天解鎖事件
- 新增路由 /activity/audit/pos-unlock（meta.requiresPermission='ACTIVITY_PAYMENT_APPROVE'）
- API: unlockPOSDailyClose payload 加 is_admin_override；新增 getPOSUnlockEvents
- 解鎖 response 含 notification_delivered=false 時提示「請私下告知對方」

對應 spec：ivy-backend/docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git log -1 --oneline
```

---

## Task 10: 整合驗證（Golden Path）

**Repo:** `both`

### - [ ] Step 1: 啟動兩端

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

開兩個瀏覽器分頁（不同 cookie / 隱身視窗）：
- 後端 API 文件：http://localhost:8088/docs
- 前端：http://localhost:5173

預先建立測試帳號（從後端 admin → 員工管理 → 新增）：
- `approver_a`：role='staff'，permissions='ACTIVITY_PAYMENT_APPROVE' + ACTIVITY_READ + WRITE
- `approver_b`：同上
- `admin/admin123` 預設應該已具 admin role

### - [ ] Step 2: Golden Path 1 — 4-eye 一般解鎖

1. 用 `approver_a` 登入 → POS 收銀某天收 NT$500 → 進日結簽核 → 簽核
2. 用 `approver_a` 同視窗點「解鎖」 → 預期跳 `ElMessageBox.alert`「您是原簽核人...」
3. 切 `approver_b` 登入 → 進日結簽核 → 點「解鎖」 → 看到 prompt 要求 ≥ 10 字
4. 輸入「B 解 A 的測試案例」 → 點確認 → 顯示「已解鎖」
5. **檢查 LINE 通知**：若 `approver_a` 有綁 LINE，應收到推播；否則前端顯示「原簽核人未綁定 LINE...」

### - [ ] Step 3: Golden Path 2 — Admin override

1. 用 `admin` 登入（具 admin role + PAYMENT_APPROVE）
2. POS 收銀某天 → 簽核（admin 自簽）
3. 點「解鎖」 → 預期出現雙確認對話框「⚠️ 以管理員身分 override...」
4. 確認 → 30 字 reason prompt → 輸入「副手請假，緊急 override 解鎖修正昨日漏記帳的部分」
5. 預期顯示「已 override 解鎖；通知已發送」

### - [ ] Step 4: Golden Path 3 — approve warnings

1. 用 `approver_a` 收銀（成為當日 operator）
2. 同 `approver_a` 進日結簽核 → 點「確認簽核」
3. 預期 toast 顯示橘色警告「你（approver_a）是當日 POS 收銀者...」
4. 簽核仍然成功（不擋）

### - [ ] Step 5: Golden Path 4 — Audit dashboard

1. 點「異常稽核軌跡」按鈕 → 跳到 `/activity/audit/pos-unlock`
2. 應看到 Step 2-4 解鎖事件 timeline，倒序排列
3. `Admin Override` 標紅、`一般解鎖` 標橘
4. 切換「近 7 天 / 30 天 / 90 天」下拉，事件數量隨之變化

### - [ ] Step 6: 後端 curl 驗證（API contract）

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"approver_a","password":"<set-password>"}' | jq -r .access_token)

# 嘗試以 approver_a 解鎖 approver_a 簽的日子 → 應 403
DATE=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d '1 day ago' +%Y-%m-%d)
curl -s -X DELETE "http://localhost:8088/api/activity/pos/daily-close/$DATE" \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"reason":"自己解自己應該被擋下","is_admin_override":false}' | jq

# 嘗試非 admin 帶 is_admin_override=true → 應 403
curl -s -X DELETE "http://localhost:8088/api/activity/pos/daily-close/$DATE" \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"reason":"假裝自己有 admin 但其實沒有所以應該要被擋下的測試","is_admin_override":true}' | jq
```

**預期 response**：第一個 403 「原簽核人」；第二個 403「僅 admin 角色」。

### - [ ] Step 7: 收尾

如有 regression：
- 後端問題回 Task 1-5 對應 Step 修，新加 commit（不 amend）
- 前端問題回 Task 6-9 對應 Step 修

完成後，在 spec 結尾標記 `**狀態**：✅ Implemented (2026-05-06)`。

---

## Self-Review Notes（plan 自查）

- [x] **Spec coverage**：spec §0 成功標準 1-8 → Task 1-4 各項；§1.1-1.4 → Task 1-4；§2.1-2.5 → Task 6-8；§3 無 migration → 跳過 ✓；§4 測試 1-10 → Task 1-4 全覆蓋；§5 風險 → 沒有對應 task（風險是設計層 acknowledgement，不需要 task）
- [x] **Placeholder scan**：Task 5 Step 2 有「兩種修法擇一」— 是合理的決策點，附了具體選擇原則。Task 6 Step 2「既有 handleUnlock 內可能有 FIELD_RULES.unlockReasonMin」— 標明需 grep 確認，可接受。Task 8 Step 2 路由格式「依既有 router 寫法對齊」— 同樣 plan 階段確認。沒有純 TBD/TODO。
- [x] **Type consistency**：
  - `is_admin_override` 一致（schema、handler、ApprovalLog comment、前端 payload）
  - `notification_delivered` 一致（response、前端讀取）
  - `admin_override` (14字) 一致（ApprovalLog action、audit endpoint filter）
  - `_UNLOCK_REASON_MIN_LENGTH=10` / `_ADMIN_OVERRIDE_REASON_MIN_LENGTH=30` 一致（後端常數、前端 minLen 參數）
- [x] **TDD flow**：Task 1-4 每個都走「寫測試 → 跑 FAIL → 實作 → 跑 PASS」順序
