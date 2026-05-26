# 才藝退費 Calculator 設計（approach B）

**日期**: 2026-05-26
**作者**: workspace brainstorming
**對應 finding**: `api/activity/pos.py:600-680` 退費路徑無建議值對照，金額由前端送入，員工算錯多退/少退無制衡。

---

## 1. 背景

對齊學費既有 `services/finance/fee_refund_calculator.py`：純函式 + 教育局三段比例。

才藝退費目前現況：
- `api/activity/pos.py` checkout（refund 路徑）僅做 advisory lock + 大額簽核 + 累積退費簽核
- `api/activity/registrations_payments.py:387` 單筆退費同樣無建議值
- `ActivityPaymentRecord.amount` 直接收前端送入值，無 calculator 算建議

**問題**：櫃台 POS 操作人員手算錯誤無稽核護欄；家長要求退費時系統無建議值對照；員工算錯多退/少退無事前制衡，事後只能從 audit log 反查（已成事實）。

---

## 2. 設計目標

1. **建議值對照**：每次退費前後端都能拿到 server-side 計算的 suggested_amount
2. **差額制衡**：實退與建議差距超閾值（預設 NT$100）強制走 ACTIVITY_PAYMENT_APPROVE 簽核
3. **與既有閘共存**：本 spec 新增「diff 簽核」，既有「總額簽核」+「累積簽核」+「原因 ≥15 字」全保留
4. **對齊學費 pattern**：純函式 calculator + router-side helper 分離，calculator 不碰 DB

**不做**：
- DB schema / migration 變動
- 前端整合（follow-up）
- 異常退費月報表（approach C，等本 spec 跑一段時間有 diff 數據再決定）

---

## 3. 退費規則（user 決策）

| 項目 | 決策 | 對應實作 |
|-----|------|---------|
| 計算規則來源 | 套教育局學費三段比例 | 沿用 `calc_enrollment_refund` 演算法 |
| 用品（教材）退費 | 一律不退（已交付） | `calc_supply_refund` 永回 0 |
| T_served 定義 | `is_present=true` 的 `ActivityAttendance` count | router-side query helper |
| T_total 定義 | `ActivityCourse.sessions`（總堂數，nullable） | `sessions IS NULL` → suggested=None + warning |
| Diff 稽核 | 絕對金額閾值 NT$100（可調） | 新 `require_approve_for_refund_diff` |
| T_served=0 特例 | 退 100%（業界慣例「未開課全退」） | calculator 內建 `ratio_band="not_started"` |

**三段比例**（與 fee_refund_calculator 完全一致）：

| served_ratio | refund_ratio | ratio_band |
|--------------|--------------|------------|
| 0（特例） | 1（全退） | `not_started` |
| <1/3 | 2/3 | `<1/3` |
| 1/3 ~ 2/3 | 1/3 | `1/3..2/3` |
| ≥2/3 | 0 | `>=2/3` |

---

## 4. 模組架構

```
services/
  activity_refund_calculator.py  ← 純函式（不碰 DB），對齊 services/finance/fee_refund_calculator.py
  activity_refund_query.py       ← router-side helper（query attendance + course.sessions → 餵 calculator）
  activity_payment_guards.py     ← +1 函式 require_approve_for_refund_diff
api/activity/
  registrations.py               ← +1 GET endpoint /{reg_id}/refund-suggestion
  pos.py                         ← refund 路徑加 server-side verify
  registrations_payments.py      ← 單筆退費路徑加 server-side verify
utils/
  activity_constants.py          ← +1 const ACTIVITY_REFUND_DIFF_THRESHOLD = 100
schemas/
  activity_admin.py              ← +1 response schema RefundSuggestionResponse
tests/
  test_activity_refund_calculator.py
  test_activity_refund_query.py
  test_activity_refund_diff_verify.py
```

---

## 5. 純函式 Calculator 契約

`services/activity_refund_calculator.py`：

```python
"""才藝退費計算 — 純函式 helpers

對齊 services/finance/fee_refund_calculator.py 的回傳形狀。
- 課程：教育局三段比例（按已出席堂數）+ T_served=0 特例 100%
- 用品：一律不退（已交付）
"""

from __future__ import annotations

from utils.rounding import round_half_up


def calc_course_refund(*, amount_due: int, T_total: int, T_served: int) -> dict:
    """計算課程退費建議金額。

    Args:
        amount_due: 課程原始金額（price_snapshot，整數元）
        T_total: 課程總堂數（ActivityCourse.sessions）；必須 > 0
        T_served: 學生已出席堂數（is_present=true 的 ActivityAttendance count）

    Raises:
        ValueError: T_total <= 0

    Returns:
        {
          "suggested_amount": int,
          "calc_method": "activity_course_ratio",
          "calc_payload": {
            "T_total": int,
            "T_served": int,
            "served_ratio": float,        # round_half_up(ratio, 4)
            "ratio_band": str,             # "not_started" | "<1/3" | "1/3..2/3" | ">=2/3"
            "refund_ratio": str,           # "1" | "2/3" | "1/3" | "0"
            "amount_due": int,
            "formula": str,
          },
          "warnings": list[str],
        }
    """
```

**規則摘要**（與 fee `calc_enrollment_refund` 對齊 + T_served=0 特例）：

| 條件 | 動作 |
|-----|-----|
| `T_total <= 0` | `raise ValueError` |
| `T_served < 0` | clamp 0 |
| `T_served > T_total` | clamp T_total |
| `T_served == 0` | suggested = amount_due（全退）, ratio_band="not_started" |
| `0 < ratio < 1/3` | suggested = round_half_up(amount_due × 2/3) |
| `1/3 ≤ ratio < 2/3` | suggested = round_half_up(amount_due × 1/3) |
| `ratio ≥ 2/3` | suggested = 0 |

```python
def calc_supply_refund(*, amount_due: int) -> dict:
    """用品（教材）退費 — 一律不退（已交付）。

    Returns:
        suggested_amount=0, calc_method="activity_supply_no_refund",
        warnings=["用品（教材）已交付，不予退費"]
    """
```

---

## 6. Router-side Query Helper

`services/activity_refund_query.py`：

```python
def build_refund_suggestion(session, reg_id: int) -> dict:
    """組裝 registration-level 退費建議。

    對每門 status='enrolled' 的 RegistrationCourse：
      T_served = SELECT COUNT(*) FROM activity_attendances aa
                 JOIN activity_sessions s ON s.id = aa.session_id
                 WHERE aa.registration_id = :reg_id
                   AND s.course_id = :course_id
                   AND aa.is_present = TRUE
      T_total  = ActivityCourse.sessions
      amount_due = RegistrationCourse.price_snapshot
      若 T_total IS NULL：item.suggested_amount=None + warning「課程未設定總堂數，
                            採保守 fallback 為 amount_due（全退）」
      否則 call calc_course_refund

    對每筆 RegistrationSupply：call calc_supply_refund(amount_due=price_snapshot)

    Returns:
      {
        "registration_id": int,
        "computed_at": iso datetime,
        "total_suggested_amount": int,   # 詳見下方算法
        "total_amount_due": int,
        "items": [
          {
            "type": "course" | "supply",
            "target_id": int,            # course_id 或 supply_id
            "name": str,
            "amount_due": int,
            "suggested_amount": int | None,  # None = 無法計算（sessions=NULL）
            "calc_method": str,
            "calc_payload": dict,
            "warnings": list[str],
          },
          ...
        ],
      }

    total_suggested_amount 算法（給前端顯示 + POS verify 雙用途）：
      total = sum(
        item.suggested_amount if item.suggested_amount is not None
        else item.amount_due           # NULL sessions fallback: 保守當全退
        for item in items
      )
    Why 用 amount_due fallback 而非 0：避免「actual=全額 / suggested=部分 → 假性
    diff 假性簽核」；NULL sessions 是 data quality 問題，遇到時保守當全退反而會在
    員工少退時觸發 diff verify（合理：admin 應該被叫來處理 NULL sessions 設定）。
    """
```

**邊界處理**：
- `RegistrationCourse.status != "enrolled"`（waitlist / promoted_pending）→ 略過，不出現在 items
- `ActivityCourse.sessions IS NULL` → item.suggested_amount=None + warning；total_suggested_amount **以 amount_due fallback**（見上方算法）
- `ActivityCourse.is_active=False`（軟刪課程） → 仍出現在 items（歷史報名仍要算）
- 用品 amount_due=price_snapshot（與 fee 一樣信任 snapshot，不重查 ActivitySupply.price）

---

## 7. GET Endpoint

`api/activity/registrations.py` 新增：

```
GET /api/activity/registrations/{reg_id}/refund-suggestion
Permission: ACTIVITY_WRITE  (= 既有退費路徑同層；POSCheckoutRequest 與 add_registration_payment 兩者皆用 ACTIVITY_WRITE)
```

**Response schema**（`schemas/activity_admin.py` 新增 `RefundSuggestionResponse`）：

```python
class RefundSuggestionItem(BaseModel):
    type: Literal["course", "supply"]
    target_id: int
    name: str
    amount_due: int
    suggested_amount: int | None
    calc_method: str
    calc_payload: dict
    warnings: list[str]

class RefundSuggestionResponse(BaseModel):
    registration_id: int
    computed_at: datetime
    total_suggested_amount: int
    total_amount_due: int
    items: list[RefundSuggestionItem]
```

**錯誤回應**：
- reg_id 不存在 / `is_active=False` → 404
- 無權限 → 403（既有 require_permission 處理）

---

## 8. POS Server-side Verify

兩個進入點（**信任 DB 不信前端**，server-side 重算）。

**確認的現行 body 結構**：`POSCheckoutItem` 僅有 `registration_id: int` + `amount: int`，**不拆 course / supply**。Verify 因此以 **reg-level** 比對；單一 reg 的 suggested 仍由 `build_refund_suggestion` 從 items 加總（含 NULL fallback，見第 6 節）。

### 8.1 `api/activity/pos.py` checkout refund 路徑

位置：在既有「第二道：每 reg 累積退費簽核」（pos.py:625-648）之後加：

```python
# ── 第三道：實退 vs 建議值偏離簽核 ──────────────
# Why: 員工算錯 / 故意多退私吞。重算 server-side suggestion 與 body 比對；
# 偏離總額 > NT$100 需 ACTIVITY_PAYMENT_APPROVE 權限（兩道獨立 — 既有總額簽核
# 擋大筆退費，本道擋偏離建議值）。
if body.type == "refund":
    actual_by_reg = {it.registration_id: it.amount for it in body.items}
    suggested_by_reg: dict[int, int] = {}
    suggestion_details: list[dict] = []
    for reg_id in actual_by_reg:
        suggestion = build_refund_suggestion(session, reg_id)
        suggested_by_reg[reg_id] = suggestion["total_suggested_amount"]
        suggestion_details.append(suggestion)

    total_actual = sum(actual_by_reg.values())
    total_suggested = sum(suggested_by_reg.values())
    # diff 累加 per-reg 絕對值，避免「多 reg 多退/少退方向抵消」漏網。
    # 例：reg1 多退 60 + reg2 少退 60，naive abs(total) = 0；正確應 = 120。
    diff = sum(
        abs(actual_by_reg[rid] - suggested_by_reg[rid])
        for rid in actual_by_reg
    )

    require_approve_for_refund_diff(
        diff=diff,
        current_user=current_user,
        suggested_total=total_suggested,
        actual_total=total_actual,
    )

    # 暫存 verify 結果，於既有 audit_changes 寫入（見第 12 節）
    _refund_audit_context = {
        "suggested_total": total_suggested,
        "actual_total": total_actual,
        "diff": diff,
        "suggestion_details": suggestion_details,
    }
```

**注意**：`require_approve_for_refund_diff` 內部已判斷 `diff <= threshold` 直接 return，無需在 caller 額外 `if diff > 0` 包裝。

### 8.2 `api/activity/registrations_payments.py:387` 單筆退費

同樣模式：在 `require_approve_for_large_refund(...)` 之後加 verify。本 endpoint 是 reg-level 單筆退費，body 含 `registration_id` + `amount` → 對該 reg call `build_refund_suggestion` → 比對 `total_suggested_amount` vs `amount`。

---

## 9. 新 Guard 函式

`services/activity_payment_guards.py` 新增：

```python
from utils.activity_constants import (
    ACTIVITY_REFUND_DIFF_THRESHOLD,
    REFUND_APPROVAL_THRESHOLD,
)


def require_approve_for_refund_diff(
    *,
    diff: int,
    current_user,
    suggested_total: int,
    actual_total: int,
) -> None:
    """實退與 calculator 建議值差距超 ACTIVITY_REFUND_DIFF_THRESHOLD（預設 NT$100）
    時，要求 ACTIVITY_PAYMENT_APPROVE 權限。

    Why: 員工算錯多退/少退無事前制衡；diff 大表示偏離教育局規則或建議值，
    需要管理者批准（既有 require_approve_for_large_refund 擋總額，本函式擋
    「偏離」，兩道獨立）。
    """
    if diff <= ACTIVITY_REFUND_DIFF_THRESHOLD:
        return
    if has_payment_approve(current_user):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"實退 NT${actual_total} 與系統建議 NT${suggested_total} 差 NT${diff}，"
            f"超過 NT${ACTIVITY_REFUND_DIFF_THRESHOLD} 偏離門檻，"
            f"需具備 ACTIVITY_PAYMENT_APPROVE 權限"
        ),
    )
```

`utils/activity_constants.py` 新增：

```python
# 實退 vs calculator 建議值差距閾值（NT$）；超過此差距需 ACTIVITY_PAYMENT_APPROVE 權限
# Why: 員工算錯/故意多退之事前制衡；與 REFUND_APPROVAL_THRESHOLD（總額）獨立，
# 兩道閘共存，任一觸發都要簽核。
ACTIVITY_REFUND_DIFF_THRESHOLD = 100
```

---

## 10. 邊界處理（必測）

| # | 情境 | 行為 |
|---|------|------|
| 1 | `ActivityCourse.sessions IS NULL` | item.suggested_amount=None + warning「採保守 fallback」；total_suggested **以 amount_due 加總**（見第 6 節算法）；POS verify 此 reg 等同「建議全退」，員工少退會觸發 diff 簽核（by design：admin 應補設定總堂數） |
| 2 | `RegistrationCourse.status != "enrolled"` | helper 略過，items 中不出現 |
| 3 | 用品 actual > 0 | suggested=0 → diff ≥ actual_supply_amount；通常觸發簽核（by design：員工想退用品須有 approve 權限） |
| 4 | 既有舊 ActivityPaymentRecord(type='refund') | calculator 不關心（只算 fresh suggestion）；既有累積退費簽核仍生效 |
| 5 | 多 reg 同收據 | 每 reg 各自 `build_refund_suggestion`；diff = sum(per_reg_diff) |
| 6 | `ActivityAttendance.is_present=false` | 不算 T_served（user 決策） |
| 7 | reg 含 NULL-sessions 課 + 正常課 | NULL 課不計入 diff；正常課照算 |
| 8 | reg.is_active=False（軟刪報名） | GET endpoint 404；POS 退費路徑既有 `_lock_regs` 已擋 |
| 9 | course 已軟刪（is_active=False） | helper 仍納入 items（歷史報名要算） |
| 10 | reg 全無 attendance 記錄 | T_served=0 → 全退 100% |

---

## 11. 測試覆蓋

### `tests/test_activity_refund_calculator.py`（~15 case）

**Calculator 純函式**：
1. `T_total <= 0` → ValueError
2. `T_served=0, T_total=10, amount=1000` → suggested=1000, ratio_band="not_started"
3. `T_served=1, T_total=10`（10%）→ suggested=667, ratio_band="<1/3"（round_half_up(666.67)）
4. `T_served=4, T_total=10`（40%）→ suggested=333, ratio_band="1/3..2/3"
5. `T_served=7, T_total=10`（70%）→ suggested=0, ratio_band=">=2/3"
6. exact 1/3 邊界：`T_served=4, T_total=12` → ratio=1/3 入 mid 段（>= 1/3）
7. exact 2/3 邊界：`T_served=8, T_total=12` → ratio=2/3 入 high 段
8. `T_served=-1` clamp 0 → 全退
9. `T_served=100, T_total=10` clamp 10 → 0
10. round_half_up：amount=1001/T_total=10/T_served=1 → 667.33 → 667
11. round_half_up half edge：amount=1500/3 → 500（exact，無 HALF_UP）
12. amount_due=0：suggested=0 各段都 0（無 div by zero）
13. `calc_supply_refund(amount_due=500)` → suggested=0 + warning
14. `calc_supply_refund(amount_due=0)` → suggested=0
15. calc_payload formula 字串包含正確 ratio/amount

### `tests/test_activity_refund_query.py`（~8 case，含 DB fixture）

1. reg 含 2 enrolled course + 1 supply，正常算
2. course.sessions=NULL → 該 item suggested=None + warning，total_suggested 不含此
3. waitlist course 略過
4. 0 attendance → 全部 T_served=0 → 全退 100%
5. mixed：1 course 已出席 3/10，另 1 course 0/10
6. is_present=false 不算 T_served
7. course 已軟刪（is_active=False）仍納入
8. reg 不存在 → ValueError 或 None（看 helper 設計，建議 raise）

### `tests/test_activity_refund_diff_verify.py`（~7 case，含 TestClient）

1. diff=0（員工剛好送 suggested）→ pass
2. diff=50（< 100 threshold）→ pass
3. diff=200（> threshold）+ 一線員工 → 403 with 偏離訊息
4. diff=200 + 有 ACTIVITY_PAYMENT_APPROVE → pass
5. 多 reg 收據 diff 加總：reg1 diff=60 + reg2 diff=60 → total 120 > 100 → 簽核
5b. **方向抵消防護**：reg1 多退 60（actual>suggested）+ reg2 少退 60（actual<suggested）→ naive abs(total) 會 0；spec 算法 sum(per-reg abs) = 120 → 簽核（確認算法正確）
6. 用品實退 NT$200 + course suggested=actual → diff=200（用品全 diff）→ 簽核
7. course.sessions=NULL → total_suggested 以 amount_due fallback；員工送 actual < amount_due 即觸發 diff（驗證 fallback 行為而非「跳過」）

---

## 12. Audit Trail

**對齊既有 audit pattern**：`pos.py` 在 checkout 末段已設好 `request.state.audit_changes` dict（見 pos.py:822-830 含 `receipt_no/type/total/...`）。本 spec 在退費路徑成功後**擴充該 dict**（不另闢新 attribute）：

```python
# 在既有 request.state.audit_changes = {...} 之後（pos.py:822-830）
if body.type == "refund":
    request.state.audit_changes.update({
        "refund_suggested_total": _refund_audit_context["suggested_total"],
        "refund_actual_total": _refund_audit_context["actual_total"],
        "refund_diff": _refund_audit_context["diff"],
        "refund_suggestion_per_reg": [
            {
                "registration_id": s["registration_id"],
                "total_suggested": s["total_suggested_amount"],
                "items": [
                    {
                        "type": it["type"],
                        "target_id": it["target_id"],
                        "suggested": it["suggested_amount"],
                        "calc_method": it["calc_method"],
                    }
                    for it in s["items"]
                ],
            }
            for s in _refund_audit_context["suggestion_details"]
        ],
    })
```

`registrations_payments.py` 同樣 update 既有 `request.state.audit_changes`（見該檔 audit pattern）。

事後可從 audit log 還原當時 calculator 算出什麼、員工偏離多少。

---

## 13. 範圍邊界

**本 spec 涵蓋**：
- 純函式 calculator + query helper
- GET endpoint + POS server-side verify（pos.py + registrations_payments.py）
- 新 guard + audit
- 後端 pytest 覆蓋

**Follow-up**（**不**在本 spec 範圍）：
- 前端 POS UI 整合（呼叫 GET endpoint + 顯示 suggested vs actual diff）
- 異常退費月報表（approach C；等 audit log 累積 diff 數據再評估）
- 細化 `POSCheckoutItem` 結構（加 `target_type` + `target_id` 拆 course/supply）：支援 item-level diff verify，可精準到「某 course 多退 NT$200」。本 spec 採 reg-level 簡化 wiring，跑一段時間後若發現 reg-level 漏判可細化
- `ACTIVITY_REFUND_DIFF_THRESHOLD` 改為 env-driven（對齊 config Phase 1 pattern）：目前 hardcoded NT$100 對齊既有 `REFUND_APPROVAL_THRESHOLD` 寫法

---

## 14. 不變項（既有設計保留）

- `require_refund_reason`（notes ≥ 15 字）
- `require_approve_for_large_refund`（總額 > NT$1000 簽核）
- 同 reg 累積退費簽核
- advisory lock + `_lock_regs` 行級鎖
- idempotency_key + receipt_no
- daily_close 守衛
- 軟刪 `voided_at` 軌跡

本 spec 新增「diff 簽核」為**第四道閘**，與上述完全獨立。
