# 才藝 POS Cash-Only 實作計劃

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 才藝 POS 移除「轉帳/其他」付款方式（系統未上線、設計錯誤修正），同步收緊退費原因字數與簽核盤點門檻。

**Architecture:** 後端 Pydantic schema 強制 `Literal["現金"]` + router fallback 雙保險；DB 欄位與 `by_method_json` 結構保留供未來擴充；前端移除付款方式選單與 by_method 拆分顯示。`SYSTEM_RECONCILE_METHOD = "系統補齊"` 是系統內部 sentinel，**不受影響**。

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic v2 (backend), Vue 3 + Element Plus (frontend), pytest (backend tests)

**前置 Spec:** `/Users/yilunwu/Desktop/ivy-backend/docs/superpowers/specs/2026-05-06-pos-cash-only-design.md`

---

## 執行順序總覽

| Phase | Task | 內容 | Repo |
|-------|------|------|------|
| A | 1 | POS checkout schema + router 守衛 + 測試 | backend |
| A | 2 | registrations.py 三個寫入端點收口 + 測試 | backend |
| A | 3 | `MIN_REFUND_REASON_LENGTH` 5→15 + 修舊測試 | backend |
| A | 4 | daily-close 盤點門檻守衛 + 測試 | backend |
| A | 5 | 後端註解清理 + 後端 final commit | backend |
| B | 6 | 前端 constants + composable | frontend |
| B | 7 | POSPaymentPanel + POSCheckoutPanel UI | frontend |
| B | 8 | POSReceipt + POSDailySummaryBar + POSApprovalView | frontend |
| B | 9 | 前端 final commit | frontend |
| C | 10 | start.sh 整合驗證 + golden path 手動測試 | both |

**重要：每個 Task 後端與前端各自走 `pytest` / `npm run test:unit`，每個 Task 結束有獨立 commit**

---

## Task 1: POS checkout schema 收口 + router fallback

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos.py:100-129`（POSCheckoutRequest schema）
- Modify: `api/activity/pos.py:139`（移除 `_VALID_METHODS`）
- Modify: `api/activity/pos.py:453-456`（router fallback 守衛）
- Create: `tests/test_pos_cash_only.py`（新檔，本 Task 只放 POS 部分；後續 Task 會擴充）

### - [ ] Step 1: 寫失敗測試

建立 `/Users/yilunwu/Desktop/ivy-backend/tests/test_pos_cash_only.py`：

```python
"""
test_pos_cash_only.py — 驗證 POS 系列端點僅接受現金。

對齊 spec: docs/superpowers/specs/2026-05-06-pos-cash-only-design.md
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.test_activity_pos import (  # 共用既有 fixture / helper
    setup_db_and_user,
    create_test_registration,
)


@pytest.fixture
def client_and_reg():
    """提供已登入 client + 一筆有應繳金額的 registration。"""
    user = setup_db_and_user(permission="ACTIVITY_WRITE")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {user['token']}"
    reg_id = create_test_registration(total_amount=500)
    return client, reg_id


def test_pos_checkout_rejects_transfer(client_and_reg):
    client, reg_id = client_and_reg
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "轉帳",
            "payment_date": "2026-05-06",
            "type": "payment",
        },
    )
    assert res.status_code == 422, res.text
    assert "現金" in res.text or "Literal" in res.text


def test_pos_checkout_rejects_other(client_and_reg):
    client, reg_id = client_and_reg
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "其他",
            "payment_date": "2026-05-06",
            "type": "payment",
        },
    )
    assert res.status_code == 422


def test_pos_checkout_default_cash_succeeds(client_and_reg):
    """不傳 payment_method 應預設『現金』成功。"""
    client, reg_id = client_and_reg
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": "2026-05-06",
            "type": "payment",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["payment_method"] == "現金"


def test_pos_checkout_explicit_cash_succeeds(client_and_reg):
    client, reg_id = client_and_reg
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "現金",
            "payment_date": "2026-05-06",
            "type": "payment",
        },
    )
    assert res.status_code == 201, res.text
```

> **注意**：`setup_db_and_user` 與 `create_test_registration` 是假設 `tests/test_activity_pos.py` 已有的 helper。執行 Step 2 時若 import 失敗，先看該檔實際 helper 名稱（可能叫別的，例如 `_setup_user`），調整 import。**不要新建 helper**。

### - [ ] Step 2: 跑測試確認 FAIL

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_cash_only.py -v
```

**預期**：4 個測試裡，`test_pos_checkout_rejects_transfer` 與 `test_pos_checkout_rejects_other` 會 **FAIL**（因為現在 schema 仍接受「轉帳/其他」，回 201 而非 422）。其他 2 個應 PASS。

如果測試在 import / fixture 階段就 ERROR，回 Step 1 修正 helper import。

### - [ ] Step 3: 實作 schema 收口

修改 `api/activity/pos.py`：

**(a)** L100-129 把 `POSCheckoutRequest.payment_method` 從 `Literal["現金", "轉帳", "其他"] = "現金"` 改為：

```python
payment_method: Literal["現金"] = Field(
    "現金",
    description="目前 POS 僅支援現金；payment_method 欄位保留供未來擴充",
)
```

**(b)** 刪掉 L139：

```python
_VALID_METHODS = {"現金", "轉帳", "其他"}
```

**(c)** L453-456 的 router 開頭：

把：
```python
if body.payment_method not in _VALID_METHODS:
    raise HTTPException(
        status_code=400, detail=f"不支援的付款方式：{body.payment_method}"
    )
```

改為：
```python
# Schema Literal 已收口；此處為 fallback 守衛，避免未來改 Literal 時漏改 router
if body.payment_method != "現金":
    raise HTTPException(
        status_code=400,
        detail="目前 POS 僅支援現金交易",
    )
```

### - [ ] Step 4: 跑測試確認 PASS

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_cash_only.py -v
pytest tests/test_activity_pos.py -v
```

**預期**：
- 新檔 4 個測試全綠
- `test_activity_pos.py` 內既有測試 **可能會出現少數紅燈**（用了 `payment_method="轉帳"` 的 case，例如 L343/581/1154/1298）。**這個 Task 不修這些**——下個 Task 3 會處理；先記下哪些紅。

### - [ ] Step 5: 不 commit（與 Task 2 合併 commit）

> **本 Task 暫不 commit**：因為 `test_activity_pos.py` 既有測試可能有紅，要等 Task 3 修完才一起 commit。中途不 push。

---

## Task 2: registrations.py 三個寫入端點 schema 收口

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/registrations.py`（搜尋全檔的 `payment_method=body.payment_method` 與 schema 定義）
- Modify: `tests/test_pos_cash_only.py`（追加 registrations 端點測試）

**已知收口位置**（plan 階段定位，實作時再以 grep 確認）：
- L1612-1656：`update_registration` 補齊欠費路徑（`body.payment_method` 來自 `PUT /registrations/{id}` 的 schema）
- L2389-2398：`add_registration_payment` 端點（schema 應有 `payment_method` 欄位）
- L2811-2820 範圍：另一個系統路徑（用 `SYSTEM_RECONCILE_METHOD`，**不動**）

**先用 grep 在實作時定位完整清單**：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
grep -nE "payment_method" api/activity/registrations.py
```

過濾出**員工輸入路徑**（schema 欄位 + 來自 `body.` 的賦值），排除 `SYSTEM_RECONCILE_METHOD`。

### - [ ] Step 1: 寫失敗測試

追加到 `tests/test_pos_cash_only.py` 末尾：

```python
def test_add_registration_payment_rejects_transfer(client_and_reg):
    """後台 POST /registrations/{id}/payments 同樣拒絕『轉帳』。"""
    client, reg_id = client_and_reg
    res = client.post(
        f"/api/activity/registrations/{reg_id}/payments",
        json={
            "type": "payment",
            "amount": 500,
            "payment_date": "2026-05-06",
            "payment_method": "轉帳",
            "notes": "test",
        },
    )
    assert res.status_code == 422, res.text


def test_add_registration_payment_default_cash(client_and_reg):
    client, reg_id = client_and_reg
    res = client.post(
        f"/api/activity/registrations/{reg_id}/payments",
        json={
            "type": "payment",
            "amount": 500,
            "payment_date": "2026-05-06",
            "notes": "test",
        },
    )
    # 不傳 payment_method 應預設「現金」
    assert res.status_code in (200, 201), res.text


def test_update_registration_paid_rejects_transfer():
    """PUT /registrations/{id} 標記 is_paid=True 補齊欠費時，
    payment_method 不能是『轉帳』。"""
    user = setup_db_and_user(permission="ACTIVITY_WRITE")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {user['token']}"
    reg_id = create_test_registration(total_amount=500)  # paid_amount=0

    res = client.put(
        f"/api/activity/registrations/{reg_id}",
        json={
            "is_paid": True,
            "payment_method": "轉帳",
            "payment_reason": "家長已付（測試）",
        },
    )
    assert res.status_code == 422, res.text
```

### - [ ] Step 2: 跑測試確認 FAIL

```bash
pytest tests/test_pos_cash_only.py::test_add_registration_payment_rejects_transfer \
       tests/test_pos_cash_only.py::test_update_registration_paid_rejects_transfer -v
```

**預期**：兩個 FAIL（現在 schema 接受「轉帳」）。

### - [ ] Step 3: 實作收口

**(a)** 找到 `add_registration_payment` 的請求 schema（搜 class name 含 `Payment` 且 schema 在 `registrations.py` 上半段，或 import 自 `_shared.py`）。把它的 `payment_method` 欄位改為：

```python
payment_method: Literal["現金"] = Field("現金", description="目前才藝 POS 僅支援現金；保留欄位供未來擴充")
```

或若該欄位是 `Optional[str]`，改為 `Optional[Literal["現金"]] = "現金"`。實作時看實際 schema 形狀。

**(b)** 找到 `update_registration` 的 schema（含 `is_paid` 與 `payment_method` 欄位），把 `payment_method` 同樣收口為 `Literal["現金"]`。

**(c)** 不要動 `SYSTEM_RECONCILE_METHOD` 相關的硬編路徑（L182、L1689、L2144、L2816）—— 那些是系統內部路徑，員工無法觸發。

**(d)** L1615-1632 的人工 method_cleaned 檢查可保留（即使 schema 收口後此檢查冗餘，留著做防呆無害）；但更新該段落的錯誤訊息說明：

把 L1620-1623 的：
```python
"請於 payment_method 填入實際收款方式"
"（如：現金/轉帳/其他），不接受系統補齊"
```
改為：
```python
"請於 payment_method 填入「現金」"
"（目前才藝僅收現金），不接受系統補齊"
```

### - [ ] Step 4: 跑測試確認 PASS

```bash
pytest tests/test_pos_cash_only.py -v
```

**預期**：全綠。

### - [ ] Step 5: 不 commit（等 Task 3 完成）

---

## Task 3: `MIN_REFUND_REASON_LENGTH` 5 → 15 + 修舊測試

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/_shared.py:55`
- Modify: `tests/test_activity_pos.py`（修舊測試 fixture 把「轉帳」case 改現金或刪除；修退費原因 fixture）
- Modify: `tests/test_pos_cash_only.py`（追加退費原因長度測試）

### - [ ] Step 1: 寫失敗測試

追加到 `tests/test_pos_cash_only.py`：

```python
def test_pos_refund_rejects_short_reason(client_and_reg):
    """退費原因 14 字 → 400（< 15 字）。"""
    client, reg_id = client_and_reg
    # 先補一筆現金繳費讓 paid_amount > 0
    client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": "2026-05-06",
        },
    )
    # 退費，原因只 14 字
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 100}],
            "payment_date": "2026-05-06",
            "type": "refund",
            "notes": "x" * 14,
        },
    )
    assert res.status_code == 400, res.text
    assert "15" in res.json()["detail"]


def test_pos_refund_accepts_15_char_reason(client_and_reg):
    client, reg_id = client_and_reg
    client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": "2026-05-06",
        },
    )
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 100}],
            "payment_date": "2026-05-06",
            "type": "refund",
            "notes": "家長要求退費學費調整事由說明清楚",  # 15 字
        },
    )
    assert res.status_code == 201, res.text
```

### - [ ] Step 2: 跑測試確認 FAIL

```bash
pytest tests/test_pos_cash_only.py::test_pos_refund_rejects_short_reason -v
```

**預期**：FAIL（目前 5 字已通過，14 字當然通過）。

### - [ ] Step 3: 實作 + 修既有測試

**(a)** 修改 `api/activity/_shared.py:55`：

```python
# 退費必填原因最短字數（避免「客人退」等敷衍；15 字強迫填寫具體事由）
MIN_REFUND_REASON_LENGTH = 15
```

**(b)** 跑舊測試找紅燈：

```bash
pytest tests/test_activity_pos.py -v 2>&1 | grep -E "FAILED|ERROR"
```

預期紅燈類型：
- 用 `payment_method="轉帳"` 的 case（Task 1+2 已收口，這裡會 422）
- 用退費 `notes` 短於 15 字的 case

**(c)** 逐一修舊測試 fixture：

對 **轉帳 case**（L343, L581, L1154, L1298 等）有兩種處理方式，**選哪個依測試本意**：
- 若測試本意是「驗證 by_method 拆分能正確分類多種 method」→ 改為直接用 `_create_payment` helper 寫 DB（繞過 API），保留斷言但更新註解：「本測試直接寫 DB 模擬歷史/系統紀錄，POS API 端點已禁止員工選擇『轉帳』」
- 若測試本意是「測 POS API 接受多種付款方式」→ 改為「測 POS API 拒絕『轉帳』」（與 test_pos_cash_only.py 重複時可刪除）

對 **退費原因短** 的 case：把 fixture 字串長度補到 ≥ 15 字。

```bash
# 範例命令找出退費原因短的 case
grep -nE 'type["\']?\s*[:=]\s*["\']?refund' tests/test_activity_pos.py
```

逐一檢視。

### - [ ] Step 4: 跑全 backend 測試確認 PASS

```bash
pytest tests/test_pos_cash_only.py tests/test_activity_pos.py -v
```

**預期**：全綠。

### - [ ] Step 5: 不 commit（等 Task 4-5）

---

## Task 4: daily-close 盤點門檻守衛

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos_approval.py:38-44`（加常數）
- Modify: `api/activity/pos_approval.py:280-300`（approve_daily_close 加守衛）
- Modify: `tests/test_pos_cash_only.py`（追加日結盤點測試）

### - [ ] Step 1: 寫失敗測試

追加到 `tests/test_pos_cash_only.py`：

```python
def test_daily_close_requires_cash_count_at_threshold(client_and_reg):
    """淨現金 ≥ NT$3,000 時，actual_cash_count 必填。"""
    client, reg_id = client_and_reg
    # 用簽核權限測試
    user = setup_db_and_user(permission="ACTIVITY_PAYMENT_APPROVE")
    client.headers["Authorization"] = f"Bearer {user['token']}"

    # 先用 POS 收 3,000 元
    reg2 = create_test_registration(total_amount=3000)
    client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg2, "amount": 3000}],
            "payment_date": "2026-05-06",
        },
    )

    # 嘗試簽核，不傳 actual_cash_count
    res = client.post(
        "/api/activity/pos/daily-close/2026-05-06",
        json={"note": "test"},
    )
    assert res.status_code == 400, res.text
    assert "盤點" in res.json()["detail"] or "actual_cash_count" in res.json()["detail"]


def test_daily_close_below_threshold_skips_cash_count(client_and_reg):
    """淨現金 < NT$3,000 時，actual_cash_count 可省略。"""
    user = setup_db_and_user(permission="ACTIVITY_PAYMENT_APPROVE")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {user['token']}"

    reg2 = create_test_registration(total_amount=2000)
    client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg2, "amount": 2000}],
            "payment_date": "2026-05-06",
        },
    )

    res = client.post(
        "/api/activity/pos/daily-close/2026-05-06",
        json={"note": "small day"},
    )
    assert res.status_code == 201, res.text


def test_daily_close_with_cash_count_succeeds_at_threshold():
    """≥ 3,000 但有填 actual_cash_count → 成功。"""
    user = setup_db_and_user(permission="ACTIVITY_PAYMENT_APPROVE")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {user['token']}"

    reg = create_test_registration(total_amount=3500)
    client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg, "amount": 3500}],
            "payment_date": "2026-05-06",
        },
    )
    res = client.post(
        "/api/activity/pos/daily-close/2026-05-06",
        json={"note": "ok", "actual_cash_count": 3500},
    )
    assert res.status_code == 201, res.text
    assert res.json()["cash_variance"] == 0
```

### - [ ] Step 2: 跑測試確認 FAIL

```bash
pytest tests/test_pos_cash_only.py::test_daily_close_requires_cash_count_at_threshold -v
```

**預期**：FAIL（現在無守衛，會 201 而非 400）。

### - [ ] Step 3: 實作守衛

修改 `api/activity/pos_approval.py`。

**(a)** 在 L38 附近加常數：

```python
# 簽核時必填現金盤點的門檻：當日預期現金 ≥ NT$3,000 時才強制
# Why: 小金額日子強迫盤點會操作疲勞；大金額日子要求對齊銀行/抽屜
_CASH_COUNT_REQUIRED_THRESHOLD = 3000
```

**(b)** 在 L280 之後（`compute_daily_snapshot` 算完 snap 之後）插入守衛：

```python
snap = compute_daily_snapshot(session, target)
by_method_net = snap["by_method_net"]
cash_snapshot = int(by_method_net.get(_CASH_METHOD_KEY, 0))

# ── 盤點門檻守衛 ──────────────────────────────────────
# 預期現金 ≥ 門檻時 actual_cash_count 必填，避免大金額日子無盤點直接過簽
if (
    cash_snapshot >= _CASH_COUNT_REQUIRED_THRESHOLD
    and body.actual_cash_count is None
):
    raise HTTPException(
        status_code=400,
        detail=(
            f"當日預期現金 NT${cash_snapshot:,} ≥ "
            f"NT${_CASH_COUNT_REQUIRED_THRESHOLD:,}，必須填寫實際現金盤點金額"
        ),
    )

cash_variance = None
if body.actual_cash_count is not None:
    cash_variance = body.actual_cash_count - cash_snapshot
```

> 注意：`cash_snapshot` 計算與既有邏輯（L282）一致，只是把守衛放在 `cash_variance` 計算之前。

### - [ ] Step 4: 跑測試確認 PASS

```bash
pytest tests/test_pos_cash_only.py -v
pytest tests/test_activity_pos.py -v
```

**預期**：新增 3 個盤點門檻測試全綠；既有日結相關測試（若有 ≥ 3,000 case 沒填 cash_count）會紅，需修 fixture 加 `actual_cash_count` 欄位。

### - [ ] Step 5: 不 commit（等 Task 5）

---

## Task 5: 後端註解清理 + 後端 final commit

**Repo:** `ivy-backend`

**Files:**
- Modify: `api/activity/pos.py:69-70`（移除誤導註解）
- Modify: `models/activity.py:475-477`（更新 by_method_json comment）
- Modify: `api/activity/_shared.py` `compute_daily_snapshot` docstring

### - [ ] Step 1: 清理 pos.py L69-70

把：
```python
# 冪等 key 有效視窗（秒）：此期間內同 key 視為重試
_IDEMPOTENCY_WINDOW_SECONDS = 600
```
改為：
```python
# 冪等 key 為全域 UNIQUE（DB 層約束）；同 key 永遠 replay 同結果
# Why: 過去用 10 分鐘 window 過濾 helper 查詢，與 DB UNIQUE 不一致導致 race；
# 現移除 window，純依 DB 約束；保留常數但僅作文件說明用途
_IDEMPOTENCY_WINDOW_SECONDS = 600  # 已不使用，保留供未來監控查詢
```

### - [ ] Step 2: 更新 models/activity.py L475-477

把：
```python
by_method_json = Column(
    Text, nullable=False, default="{}", comment="分付款方式 JSON"
)
```
改為：
```python
by_method_json = Column(
    Text,
    nullable=False,
    default="{}",
    comment="分付款方式 JSON（目前才藝僅收現金，保留 JSON 結構 {method:net} 供未來擴充）",
)
```

### - [ ] Step 3: 更新 _shared.py `compute_daily_snapshot` docstring

L1034-1041 把：
```python
def compute_daily_snapshot(session, target_date: date) -> dict:
    """某日 POS 流水即時快照：payment_total / refund_total / net_total / transaction_count / by_method。

    供 POS daily-summary 端點與日結簽核共用，避免邏輯雙寫。
    by_method 為 dict：{"現金": 1200, "轉帳": 500, ...}；method 為 NULL 者歸類為「未指定」。

    Voided 紀錄（軟刪）一律排除，避免讓老闆簽核的總額包含已被作廢的交易。
    """
```
改為：
```python
def compute_daily_snapshot(session, target_date: date) -> dict:
    """某日 POS 流水即時快照：payment_total / refund_total / net_total / transaction_count / by_method。

    供 POS daily-summary 端點與日結簽核共用，避免邏輯雙寫。
    by_method 為 dict：員工輸入只可能是「現金」；系統內部沖帳會出現「系統補齊」；
    method 為 NULL 者歸類為「未指定」（歷史資料）。

    Voided 紀錄（軟刪）一律排除，避免讓老闆簽核的總額包含已被作廢的交易。
    """
```

### - [ ] Step 4: 跑全 backend 測試最後一次確認

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_pos_cash_only.py tests/test_activity_pos.py tests/test_finance_antitheft_round3.py -v
```

**預期**：全綠。

### - [ ] Step 5: 後端 commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/activity/pos.py api/activity/registrations.py api/activity/_shared.py \
        api/activity/pos_approval.py models/activity.py \
        tests/test_pos_cash_only.py tests/test_activity_pos.py
git status   # 檢查無多餘檔案
git commit -m "$(cat <<'EOF'
feat(activity-pos): 限制才藝 POS 僅收現金 + 收緊退費/盤點守衛

系統未上線，「轉帳/其他」付款方式為設計錯誤；移除以杜絕員工偽造轉帳
記錄私吞現金的舞弊路徑（spec C1）。

- POSCheckoutRequest.payment_method 收口為 Literal["現金"]，router fallback 雙保險
- registrations.py 三個員工輸入端點同步收口（不影響系統內部 SYSTEM_RECONCILE_METHOD）
- MIN_REFUND_REASON_LENGTH 5 → 15，避免「客人退」敷衍
- daily-close 預期現金 ≥ NT$3,000 時 actual_cash_count 必填
- 註解清理：by_method_json、IDEMPOTENCY_WINDOW、compute_daily_snapshot docstring
- 新增 tests/test_pos_cash_only.py；修舊測試 fixture 把「轉帳」case 改現金或繞 API

對應 spec：docs/superpowers/specs/2026-05-06-pos-cash-only-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git status
```

---

## Task 6: 前端 constants + composable

**Repo:** `ivy-frontend`

**Files:**
- Modify: `src/constants/pos.js:9-13`（移除 POS_PAYMENT_METHODS）
- Modify: `src/composables/usePOSCheckout.js:70, 108, 314, 258`（移除 paymentMethod ref/options/payload/reset）

### - [ ] Step 1: 修改 `src/constants/pos.js`

把 L9-13：
```javascript
export const POS_PAYMENT_METHODS = [
  { value: '現金', label: '現金' },
  { value: '轉帳', label: '轉帳' },
  { value: '其他', label: '其他' },
]
```
**整段刪除**。

`CASH_METHOD = '現金'`（L20）保留。

在 `CASH_METHOD` 上方加註解：
```javascript
// 才藝 POS 僅收現金（spec 2026-05-06-pos-cash-only-design.md）
// 若未來擴充，後端 schema (api/activity/pos.py POSCheckoutRequest.payment_method)
// 與此處需同步
export const CASH_METHOD = '現金'
```

### - [ ] Step 2: 修改 `src/composables/usePOSCheckout.js`

**(a)** 移除 import 中的 `POS_PAYMENT_METHODS`（L11-17 的 import 區塊）：

從：
```javascript
import {
  CASH_METHOD,
  LARGE_AMOUNT_THRESHOLD,
  POS_PAYMENT_METHODS,
  computeOwed,
  formatTWD,
} from '@/constants/pos'
```
改為：
```javascript
import {
  CASH_METHOD,
  LARGE_AMOUNT_THRESHOLD,
  computeOwed,
  formatTWD,
} from '@/constants/pos'
```

**(b)** L70 `const paymentMethod = ref(CASH_METHOD)` **保留**（很多地方還在引用）；但加註解：
```javascript
// 永遠是現金；保留 ref 以便未來擴充時最小改動
const paymentMethod = ref(CASH_METHOD)
```

**(c)** L108 `const paymentMethodOptions = POS_PAYMENT_METHODS` **整行刪除**。

**(d)** L258 `reset()` 內 `paymentMethod.value = CASH_METHOD` **保留**（不變，仍歸位）。

**(e)** L275 把 `cleanedNotes.length < 5` 改為 `cleanedNotes.length < 15`，並更新訊息：

從：
```javascript
if (isRefundMode.value && cleanedNotes.length < 5) {
  ElMessage.warning('退費必須於備註填寫原因（至少 5 個字）')
  return
}
```
改為：
```javascript
if (isRefundMode.value && cleanedNotes.length < 15) {
  ElMessage.warning('退費必須於備註填寫具體原因（至少 15 個字）')
  return
}
```

**(f)** L314 payload `payment_method: paymentMethod.value` **不改**（值永遠是「現金」，後端會檢查）。

**(g)** L470-471 export 物件中 `paymentMethodOptions` 移除：

從：
```javascript
paymentMethod,
paymentMethodOptions,
notes,
```
改為：
```javascript
paymentMethod,
notes,
```

**(h)** `canSubmit` computed (L97-106) 加退費備註長度 hard block：

從：
```javascript
const canSubmit = computed(() => {
  if (submitting.value) return false
  const item = selectedItem.value
  if (!item) return false
  const applied = Number(item.amount_applied) || 0
  if (applied <= 0) return false
  // 退費模式：金額不得超過已繳
  if (isRefundMode.value && applied > (item.paid_amount || 0)) return false
  return true
})
```
改為：
```javascript
const canSubmit = computed(() => {
  if (submitting.value) return false
  const item = selectedItem.value
  if (!item) return false
  const applied = Number(item.amount_applied) || 0
  if (applied <= 0) return false
  // 退費模式：金額不得超過已繳；且原因 ≥ 15 字 hard block
  if (isRefundMode.value) {
    if (applied > (item.paid_amount || 0)) return false
    if ((notes.value || '').trim().length < 15) return false
  }
  return true
})
```

### - [ ] Step 3: 跑型別 / lint 確認

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run lint
npm run test:unit -- usePOSCheckout 2>&1 | tail -30
```

**預期**：lint 通過；如果有 unit test 涉及 `paymentMethodOptions` 應失敗，下個 Task 修。

### - [ ] Step 4: 不 commit（等 Task 7-8）

---

## Task 7: POSPaymentPanel + POSCheckoutPanel UI

**Repo:** `ivy-frontend`

**Files:**
- Modify: `src/components/activity/POSPaymentPanel.vue:85-102, 162-163, 174`
- Modify: `src/components/activity/POSCheckoutPanel.vue:22-37, 186-187`

### - [ ] Step 1: 修改 `POSPaymentPanel.vue`

**(a)** L85-102 整段「付款方式」radio 區塊**刪除**：

從：
```html
    <div class="pos-payment__field">
      <label class="pos-payment__label">
        {{ isRefundMode ? '退款方式' : '付款方式' }}
      </label>
      <el-radio-group
        :model-value="paymentMethod"
        size="large"
        @update:model-value="$emit('update:paymentMethod', $event)"
      >
        <el-radio-button
          v-for="m in paymentMethodOptions"
          :key="m.value"
          :value="m.value"
        >
          {{ m.label }}
        </el-radio-button>
      </el-radio-group>
    </div>
```
改為**完全刪除**（不保留任何 placeholder div）。

**(b)** L116-127 備註欄位的 placeholder 與 maxlength：

從：
```html
      <el-input
        :model-value="notes"
        type="textarea"
        :rows="2"
        maxlength="200"
        show-word-limit
        :placeholder="isRefundMode ? '例如：家長要求退費、事由' : ''"
        @update:model-value="$emit('update:notes', $event)"
      />
```
改為：
```html
      <el-input
        :model-value="notes"
        type="textarea"
        :rows="2"
        maxlength="200"
        show-word-limit
        :placeholder="isRefundMode ? '退費原因（至少 15 字，例如：家長申請退費，原因為…）' : ''"
        @update:model-value="$emit('update:notes', $event)"
      />
```

**(c)** L161-171 props 移除 `paymentMethod` 與 `paymentMethodOptions`：

從：
```javascript
defineProps({
  paymentMethod: { type: String, required: true },
  paymentMethodOptions: { type: Array, required: true },
  itemTotal: { type: Number, required: true },
  selectedItem: { type: Object, default: null },
  notes: { type: String, default: '' },
  canSubmit: { type: Boolean, required: true },
  submitting: { type: Boolean, required: true },
  checkoutType: { type: String, default: 'payment' },
  isRefundMode: { type: Boolean, default: false },
})
```
改為：
```javascript
defineProps({
  itemTotal: { type: Number, required: true },
  selectedItem: { type: Object, default: null },
  notes: { type: String, default: '' },
  canSubmit: { type: Boolean, required: true },
  submitting: { type: Boolean, required: true },
  checkoutType: { type: String, default: 'payment' },
  isRefundMode: { type: Boolean, default: false },
})
```

**(d)** L173-181 emits 移除 `update:paymentMethod`：

從：
```javascript
defineEmits([
  'update:paymentMethod',
  'update:notes',
  'update:checkoutType',
  'update:appliedAmount',
  'clear-selection',
  'clear',
  'submit',
])
```
改為：
```javascript
defineEmits([
  'update:notes',
  'update:checkoutType',
  'update:appliedAmount',
  'clear-selection',
  'clear',
  'submit',
])
```

### - [ ] Step 2: 修改 `POSCheckoutPanel.vue`

**(a)** L22-37 移除 `v-model:payment-method` 與 `:payment-method-options`：

從：
```html
      <POSPaymentPanel
        v-model:payment-method="paymentMethod"
        v-model:notes="notes"
        v-model:checkout-type="checkoutType"
        :payment-method-options="paymentMethodOptions"
        :is-refund-mode="isRefundMode"
        :item-total="itemTotal"
        :selected-item="selectedItem"
        :can-submit="canSubmit"
        :submitting="submitting"
        class="pos-panel-wrap__col"
        @update:applied-amount="updateSelectedAmount"
        @clear-selection="clearSelection"
        @clear="resetTransactionInputs"
        @submit="handleSubmit"
      />
```
改為：
```html
      <POSPaymentPanel
        v-model:notes="notes"
        v-model:checkout-type="checkoutType"
        :is-refund-mode="isRefundMode"
        :item-total="itemTotal"
        :selected-item="selectedItem"
        :can-submit="canSubmit"
        :submitting="submitting"
        class="pos-panel-wrap__col"
        @update:applied-amount="updateSelectedAmount"
        @clear-selection="clearSelection"
        @clear="resetTransactionInputs"
        @submit="handleSubmit"
      />
```

**(b)** L186-187 destructure 移除 `paymentMethod`、`paymentMethodOptions`：

從：
```javascript
  paymentMethod,
  paymentMethodOptions,
  notes,
```
改為：
```javascript
  notes,
```

**(c)** L87-89（recent transactions table）的「方式」column 移除：

從：
```html
        <el-table-column label="方式" width="70" align="center">
          <template #default="{ row }">{{ row.payment_method }}</template>
        </el-table-column>
```
**整段刪除**——既然只有現金，無需顯示這 column。

### - [ ] Step 3: 啟動 dev server 視覺驗證

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
# 另一個 terminal 開瀏覽器檢查 http://localhost:5173/activity/pos
```

確認：
- [ ] POS 主頁不再有「付款方式」radio 群組
- [ ] 備註欄退費模式 placeholder 顯示「退費原因（至少 15 字...）」
- [ ] 退費模式時，原因 < 15 字「確認退費」按鈕 disabled
- [ ] 今日交易表沒有「方式」column
- [ ] 結帳成功後 console 無 Vue warning（如 missing prop）

### - [ ] Step 4: 不 commit（等 Task 8）

---

## Task 8: POSReceipt + POSDailySummaryBar + POSApprovalView

**Repo:** `ivy-frontend`

**Files:**
- Modify: `src/components/activity/POSReceipt.vue:14`
- Modify: `src/components/activity/POSDailySummaryBar.vue:31-39, 54`
- Modify: `src/views/activity/POSApprovalView.vue:185, 540`

### - [ ] Step 1: `POSReceipt.vue` L14

把：
```html
      <div>方式：{{ receipt.payment_method }}</div>
```
改為：
```html
      <div>方式：現金</div>
```

> 既然只剩現金，硬編比動態更省事；未來擴充時再改回。

### - [ ] Step 2: `POSDailySummaryBar.vue`

**(a)** L31-39 整個 `methodBreakdown` 顯示區塊**刪除**：

從：
```html
    <div v-if="methodBreakdown.length" class="pos-daily-bar__methods">
      <span
        v-for="m in methodBreakdown"
        :key="m.method"
        class="pos-daily-bar__method-tag"
      >
        {{ m.method }} · {{ formatTWD(m.payment) }}
      </span>
    </div>
```

**(b)** L54 `methodBreakdown` computed 也刪除：

從：
```javascript
const methodBreakdown = computed(() => props.data?.by_method || [])
```
**整行刪除**。

**(c)** L65-78 對應 CSS class `.pos-daily-bar__methods` 與 `.pos-daily-bar__method-tag` **整段刪除**（既然 template 不再用）。

### - [ ] Step 3: `POSApprovalView.vue`

**(a)** L185 交易明細表的「方式」column：

```html
                <template #default="{ row }">{{ row.payment_method }}</template>
```

整個 `<el-table-column>` 區塊（含外層 label="方式" 那行）**刪除**。先 grep 找到完整邊界：

```bash
grep -n "label=\"方式\"" /Users/yilunwu/Desktop/ivy-frontend/src/views/activity/POSApprovalView.vue
```

連同上下 `<el-table-column ...>` 與 `</el-table-column>` 一起刪。

**(b)** L196-215 區域（`actual_cash_count` / `cash_variance` 顯示）**保留**——是顯示用，不變。

**(c)** L540-541 `approvePOSDailyClose` 呼叫：

從：
```javascript
    await approvePOSDailyClose(selectedDate.value, {
      note: form.note || null,
      actual_cash_count: cash == null ? null : Number(cash),
    })
```

不改 API call，但前端**送出前**加 hard block：

在這個 function 開頭（`submitting.value = true` 之前）加守衛：

```javascript
// ── 盤點門檻守衛（與後端對齊：現金 ≥ NT$3,000 時必填） ─────
const CASH_COUNT_REQUIRED_THRESHOLD = 3000
const expectedCash = Number(detail.value?.by_method?.['現金'] ?? 0)
if (expectedCash >= CASH_COUNT_REQUIRED_THRESHOLD && (cash == null || cash === '')) {
  ElMessage.warning(
    `當日預期現金 ${formatTWD(expectedCash)} ≥ ${formatTWD(CASH_COUNT_REQUIRED_THRESHOLD)}，必須填寫實際現金盤點金額`
  )
  return
}
```

> 確切位置：在 `submitting.value = true` 上方、 `try` 之前。實作時看 function 結構調整。

### - [ ] Step 4: 啟動 dev server 視覺驗證

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

確認：
- [ ] 收據列印預覽「方式：現金」
- [ ] 日結 bar 沒有「現金 · NT$X」tag pill
- [ ] 簽核頁交易明細表沒有「方式」column
- [ ] 累積收 NT$3,500 後，簽核頁不填盤點 → 點「確認簽核」彈警告，不送出 API

### - [ ] Step 5: 跑前端 unit test

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run test:unit
```

**預期**：全綠（涉及 POS 元件的測試若有寫死「轉帳」要修）。

### - [ ] Step 6: 不 commit（等 Task 9）

---

## Task 9: 前端 final commit

**Repo:** `ivy-frontend`

### - [ ] Step 1: 跑全 lint + test 最後確認

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run lint
npm run test:unit
npm run build   # 確認無 TS / build error
```

**預期**：全綠。

### - [ ] Step 2: 前端 commit

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/constants/pos.js src/composables/usePOSCheckout.js \
        src/components/activity/POSPaymentPanel.vue \
        src/components/activity/POSCheckoutPanel.vue \
        src/components/activity/POSReceipt.vue \
        src/components/activity/POSDailySummaryBar.vue \
        src/views/activity/POSApprovalView.vue
git status
git commit -m "$(cat <<'EOF'
feat(activity-pos): 移除 POS 付款方式選單與 by_method 拆分顯示

對齊後端僅收現金的政策（spec C1）；同步加上退費原因 15 字 hard block
與簽核盤點門檻 NT$3,000 前端守衛。

- 移除 POS_PAYMENT_METHODS 常數、POSPaymentPanel 付款方式 radio 群組
- POSReceipt 方式欄硬編「現金」
- POSDailySummaryBar 移除 by_method tag pill
- POSApprovalView 交易明細表移除「方式」column；簽核前盤點門檻守衛
- canSubmit 退費模式新增「原因 ≥ 15 字」hard block

對應 spec：ivy-backend/docs/superpowers/specs/2026-05-06-pos-cash-only-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git status
```

---

## Task 10: 整合驗證（Golden Path 手動測試）

**Repo:** `both`

### - [ ] Step 1: 啟動兩端

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

開兩個分頁：
- 後端 API 文件：http://localhost:8088/docs
- 前端：http://localhost:5173

用 `admin / admin123` 登入。

### - [ ] Step 2: 測試 Golden Path（小金額）

1. 進入「活動 → POS 收銀」
2. 搜尋有欠費的學生 → 選一筆 NT$500 報名
3. 確認**不再有付款方式 radio**
4. 點「確認收款並列印」 → 收據顯示「方式：現金」
5. 進「日結簽核」→ 選今天 → 預期現金 NT$500 < NT$3,000
6. 不填 actual_cash_count，點「確認簽核」 → **應成功**

### - [ ] Step 3: 測試大金額盤點門檻

1. 回 POS 收銀，再連續收三筆共 NT$3,500
2. 進「日結簽核」→ 預期現金 NT$4,000
3. 不填 actual_cash_count，點「確認簽核」 → **應彈警告（前端守衛）**
4. 填 actual_cash_count = 3,950 → 點簽核 → 顯示 cash_variance = -50 確認對話框 → 確認 → 簽核成功

### - [ ] Step 4: 測試退費 hard block

1. 解鎖剛才簽核的日子（簽核頁 → 解鎖 → 填 ≥10 字原因）
2. 回 POS 收銀，切「退費」模式
3. 對剛才 NT$500 的報名退 NT$100
4. 備註填「短」→ 「確認退費」按鈕應 **disabled**
5. 備註改填 15 字（例：「家長申請退費，本月暫停課程」）→ 按鈕亮起 → 退費成功
6. 收據顯示退費資訊與「方式：現金」

### - [ ] Step 5: 測試後端守衛（curl）

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)

# 嘗試送「轉帳」 → 期望 422
curl -s -X POST http://localhost:8088/api/activity/pos/checkout \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"items":[{"registration_id":1,"amount":100}],"payment_method":"轉帳","payment_date":"2026-05-06"}'
```

**預期 response**：HTTP 422 + Pydantic 錯誤訊息提及 `Literal["現金"]`。

### - [ ] Step 6: 收尾

回報任何發現的 regression。如有 bug：
- 後端問題回 Task 1-5 對應步驟修，新加 commit（不 amend）
- 前端問題回 Task 6-9 對應步驟修

完成後在 spec 結尾標記 `**狀態**：✅ Implemented (2026-05-06)`。

---

## Self-Review Notes（plan 自查）

- [x] **Spec coverage**：spec §1.1-1.5 → Task 1-5；§2.1-2.6 → Task 6-8；連帶優化 1/2/3 → 各章節已分配；DB / migration §3 → 無動作（spec 確認無）；§7 out-of-scope → 不在 plan
- [x] **Placeholder scan**：Step 3 of Task 2 用了「實作時看實際 schema 形狀」——這是 grep 後決定的微小 ambiguity，已附 grep 命令，可接受
- [x] **Type consistency**：`_CASH_COUNT_REQUIRED_THRESHOLD` 後端與 `CASH_COUNT_REQUIRED_THRESHOLD` 前端一致（值都是 3000）；`MIN_REFUND_REASON_LENGTH` 後端 15 ↔ 前端 hard-coded 15 一致
- [x] **Test → impl → commit** 順序在每個有測試的 Task 內 TDD 完整
