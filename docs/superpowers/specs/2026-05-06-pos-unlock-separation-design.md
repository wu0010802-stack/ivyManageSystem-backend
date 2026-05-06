# 才藝 POS 日結解鎖權限分離（Unlock 4-eye）設計

**日期**：2026-05-06
**狀態**：📝 Draft（待 brainstorming approve）
**前置稽核**：2026-05-06 對話內 POS 企業稽核 C2 項
**範圍**：跨前後端（ivy-backend + ivy-frontend）

---

## 0. 範圍與目標

### 問題陳述

目前 `pos_approval.py` 的 POST（簽核）與 DELETE（解鎖）共用單一權限 `ACTIVITY_PAYMENT_APPROVE`。
同一人可循環「簽 → 解 → 改 → 重簽」無痕修帳：

- ApprovalLog 留下軌跡，但**沒人主動稽核**
- 即使有兩個簽核者，原簽核人仍可以自簽自解，4-eye 失效

業主目前規劃 **2 人**持有 `ACTIVITY_PAYMENT_APPROVE`，且 `User.role='admin'`（系統管理員，通常 = 老闆本人）已存在於系統，足以支撐 4-eye 設計。

### 設計決策

| 維度 | 決策 |
|------|------|
| Q1 兩個簽核者 | 4-eye 可行 |
| Q2 一人請假時 | Admin role override + LINE 通知（最務實，留軌跡） |
| Q3 approve 是否也 4-eye | **否**（A 方案）：approve 沒「修帳」風險，只 unlock 有 |
| Q3 補強 | approve 在「簽核者 = 當日 POS 收銀者」時加 UI 軟提醒（C 方案） |
| Q4 通知範圍 | LINE 個人推播原簽核人 + 後台「異常稽核」dashboard（C 方案） |
| Q5 dashboard 路由 | `/activity/audit/pos-unlock`（緊耦合於才藝模組） |
| Q6 未綁 LINE flag | unlock response 加 `notification_delivered: bool` 欄位 |

### 範圍邊界

✅ **包含**：
- 後端 `unlock_daily_close` 4-eye 守衛 + admin override 路徑
- 後端 `approve_daily_close` response 增加 `warnings` 欄位（簽核者 = 當日操作者時）
- 後端 `line_service.notify_pos_unlock_to_approver()` 新方法（best-effort）
- 後端新端點 `GET /api/activity/audit/pos-unlock-events`
- 前端 `POSApprovalView` 解鎖 UI 三分支（非原簽核人 / 原簽核人非 admin / 原簽核人 admin override）
- 前端 approve 後逐條顯示 `warnings`
- 前端新 view `POSAuditEventsView.vue` + 入口連結
- 新測試 `tests/test_pos_unlock_separation.py`

❌ **不包含**：
- approve 4-eye（明確選 A 方案）
- ApprovalLog 全表 UI / 通用 audit dashboard
- 員工 LINE 綁定流程（既有 `User.line_user_id` 欄位；綁定流程屬後續工作）
- 簽核冷卻期 / retract window（Q2 D 選項未選）
- C3/C4/C5 + H 系列其他稽核 finding

### 非目標

- 不引入新權限位（沿用現有 `ACTIVITY_PAYMENT_APPROVE` + `User.role='admin'`）
- 不改 `ActivityPosDailyClose` schema
- 不改 `ApprovalLog` schema（沿用既有 `action String(20)` 欄位，新增字面值 `admin_override` = 14 字，可容納）

### 成功標準

1. 原簽核人帶 `is_admin_override=False` 解鎖自己簽過的日子 → **403**
2. 不同 `ACTIVITY_PAYMENT_APPROVE` 持有者解鎖 → **204**（既有行為不變）
3. Admin role + `is_admin_override=True` + reason ≥ 30 字 → **204**，ApprovalLog `action='admin_override'`
4. Admin override 但 reason < 30 字 → **422**
5. 非 admin 帶 `is_admin_override=True` → **403**
6. unlock 成功後若原簽核人有綁 LINE，收到推播；無則 silent，response 帶 `notification_delivered: false`
7. approve_daily_close response 在 `current_user.username` ∈ 當日 operator distinct 集合時，回傳 `warnings: ["你是當日 POS 收銀者..."]`
8. `GET /api/activity/audit/pos-unlock-events?days=30` 回傳近 30 天 unlock 事件（含 admin override）按時間倒序，限 200 筆

---

## 1. 後端變更

### 1.1 `api/activity/pos_approval.py` — schema 與守衛

**`DailyCloseUnlock` schema** 加 `is_admin_override` 旗標 + 動態最短字數：

```python
_ADMIN_OVERRIDE_REASON_MIN_LENGTH = 30
_NORMAL_UNLOCK_REASON_MIN_LENGTH = 10  # 既有

class DailyCloseUnlock(BaseModel):
    reason: str = Field(..., max_length=500)
    is_admin_override: bool = Field(
        False,
        description="管理員緊急 override：略過 4-eye 但 reason 須 ≥ 30 字",
    )

    @model_validator(mode="after")
    def _validate_reason_length(self):
        cleaned = (self.reason or "").strip()
        min_len = (
            _ADMIN_OVERRIDE_REASON_MIN_LENGTH
            if self.is_admin_override
            else _NORMAL_UNLOCK_REASON_MIN_LENGTH
        )
        if len(cleaned) < min_len:
            raise ValueError(
                f"解鎖原因需 ≥ {min_len} 字"
                f"{'（admin override 須具體說明緊急情況）' if self.is_admin_override else ''}"
            )
        self.reason = cleaned
        return self
```

**`unlock_daily_close` handler** 新增 4-eye 守衛（位置：取得 `row` 後、刪除前）：

```python
# ── 4-eye 守衛 ─────────────────────────────────────────
# Admin override 路徑：必須 role='admin'，可解鎖自己簽的（後續 LINE 通知留軌跡）
# 一般 unlock：解鎖人 ≠ 原簽核人
if body.is_admin_override:
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="僅 admin 角色可進行 override 解鎖；請改用一般 4-eye 流程",
        )
elif current_user.get("username") == row.approver_username:
    raise HTTPException(
        status_code=403,
        detail=(
            f"解鎖人不可為原簽核人 {row.approver_username}；"
            "請由其他簽核權限者執行，或以 admin 身分 override"
        ),
    )
```

**ApprovalLog action 區分**：
```python
action = "admin_override" if body.is_admin_override else "cancelled"
session.add(
    ApprovalLog(
        doc_type="activity_pos_daily",
        doc_id=_doc_id_for(target),
        action=action,
        approver_username=current_user.get("username", ""),
        approver_role=current_user.get("role"),
        comment=comment,
    )
)
```

**LINE 通知（commit 後 best-effort）**：
```python
session.commit()
# ── LINE 通知（best-effort；失敗不擋已 commit 的解鎖）─────────
delivered = False
try:
    line_svc = get_line_service()
    if line_svc:
        delivered = line_svc.notify_pos_unlock_to_approver(
            target_date=target,
            original_approver=original_approver,
            unlocker=current_user.get("username", ""),
            is_override=body.is_admin_override,
            reason=body.reason,
        )
except Exception:
    logger.warning("LINE notify on POS unlock failed", exc_info=True)
```

**Response 改為帶 body**（既有 204 改 200 + JSON），增加 `notification_delivered`：

```python
return {
    "close_date": target.isoformat(),
    "unlocked_at": datetime.now().isoformat(timespec="seconds"),
    "is_admin_override": body.is_admin_override,
    "notification_delivered": delivered,  # 原簽核人是否收到 LINE
}
```

> **API contract change**：DELETE 從 204 改為 200 + JSON。前端需同步調整解析。

### 1.2 `api/activity/pos_approval.py` — approve 軟提醒

`approve_daily_close` handler 在計算 snap 後、寫入前查當日 operator 集合：

```python
operators_today = {
    op for (op,) in session.query(ActivityPaymentRecord.operator)
    .filter(
        ActivityPaymentRecord.payment_date == target,
        ActivityPaymentRecord.voided_at.is_(None),
    )
    .distinct()
    .all()
    if op
}
warnings: list[str] = []
if current_user.get("username") in operators_today:
    warnings.append(
        f"你（{current_user.get('username')}）是當日 POS 收銀者；"
        "建議由其他簽核者覆核以強化稽核獨立性"
    )
```

`_serialize_close(row)` 包成 response 後加 `warnings`：

```python
response = _serialize_close(row)
response["warnings"] = warnings
return response
```

> 注意：既有 `_serialize_close` 不變（其他端點 GET / unlock 也呼叫它）；`warnings` 只在 POST 端點 inline append。

### 1.3 `services/line_service.py` — 新通知方法

```python
def notify_pos_unlock_to_approver(
    self,
    *,
    target_date: date,
    original_approver: str,
    unlocker: str,
    is_override: bool,
    reason: str,
) -> bool:
    """通知原簽核人：他簽過的日結被解鎖。

    Returns: True 若推播成功送出；False 若無 LINE 綁定或推播失敗。
    Best-effort：呼叫端不應因 False 中止 unlock 流程。
    """
    if not self._enabled or not self._token:
        return False
    session = get_session()
    try:
        from models.auth import User
        user = (
            session.query(User)
            .filter(User.username == original_approver, User.is_active.is_(True))
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

> 不套用家長端的 `is_pref_enabled` 偏好過濾——員工端通知是稽核必要，不可關閉。

### 1.4 `api/activity/pos_approval.py` — audit dashboard 端點

```python
@router.get("/activity/audit/pos-unlock-events")
async def list_pos_unlock_events(
    days: int = Query(30, ge=1, le=180, description="查詢過去 N 天"),
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """列出近 N 天的 POS 日結解鎖事件（一般 4-eye + admin override）。

    時間倒序，限 200 筆；ApprovalLog 為 source of truth。
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
            close_date = _doc_id_to_date(r.doc_id)
            events.append({
                "id": r.id,
                "close_date": close_date.isoformat() if close_date else None,
                "action": r.action,  # 'cancelled' or 'admin_override'
                "unlocker_username": r.approver_username,
                "unlocker_role": r.approver_role,
                "comment": r.comment,  # 含原簽核人摘要 + 原因（解鎖時手動組）
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


def _doc_id_to_date(doc_id: int):
    """將 ApprovalLog.doc_id (YYYYMMDD int) 解回 date。"""
    s = str(doc_id)
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
```

---

## 2. 前端變更

### 2.1 `src/api/activity.js` — payload 與新 API

```javascript
export const unlockPOSDailyClose = (date, payload) =>
  // payload: { reason: string, is_admin_override?: boolean }
  // response: 200 + { close_date, unlocked_at, is_admin_override, notification_delivered }
  api.delete(`/activity/pos/daily-close/${date}`, { data: payload })

export const getPOSUnlockEvents = (days = 30) =>
  api.get('/activity/audit/pos-unlock-events', { params: { days } })
```

### 2.2 `src/views/activity/POSApprovalView.vue` — unlock UI

`handleUnlock` 改為三分支：

```javascript
async function handleUnlock() {
  if (!canApprove.value) return

  const isOriginal = currentUser.username === detail.value?.approver_username
  const isAdmin = currentUser.role === 'admin'

  // 分支 1：非原簽核人 → 一般 4-eye 流程
  if (!isOriginal) {
    return doUnlock({ isOverride: false, minLen: 10 })
  }

  // 分支 2：原簽核人但非 admin → 擋下提示
  if (!isAdmin) {
    ElMessageBox.alert(
      `您是原簽核人 ${detail.value.approver_username}；解鎖必須由其他簽核者執行。\n\n` +
      '若情況緊急且您具備管理員身分，請聯繫系統管理員協助 override。',
      '無法解鎖',
      { type: 'warning' }
    )
    return
  }

  // 分支 3：原簽核人 + admin → override 路徑
  try {
    await ElMessageBox.confirm(
      '⚠️ 您是原簽核人；以管理員身分 override 解鎖將寫入特殊稽核紀錄並 LINE 通知您自己。\n\n' +
      '建議優先請其他簽核者解鎖；override 應僅用於對方不在的緊急情況。',
      'Admin Override 解鎖',
      { confirmButtonText: '我了解，繼續 override', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
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
        inputValidator: v =>
          (v || '').trim().length >= minLen || `至少 ${minLen} 字`,
      }
    )
    reason = (res.value || '').trim()
  } catch {
    return
  }

  submitting.value = true
  try {
    const { data } = await unlockPOSDailyClose(selectedDate.value, {
      reason,
      is_admin_override: isOverride,
    })
    ElMessage.success(
      isOverride ? '已 override 解鎖；通知已發送' : '已解鎖'
    )
    if (!data.notification_delivered) {
      ElMessage.warning(
        '原簽核人未綁定 LINE，未收到自動通知；請私下告知對方。'
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

### 2.3 `src/views/activity/POSApprovalView.vue` — approve warnings

`handleApprove` 成功後：

```javascript
const { data } = await approvePOSDailyClose(selectedDate.value, payload)
const warnings = data?.warnings || []
warnings.forEach(w => ElMessage.warning({ message: w, duration: 6000 }))
ElMessage.success('簽核完成')
```

### 2.4 新 view `src/views/activity/POSAuditEventsView.vue`

```vue
<template>
  <el-card>
    <div class="pos-audit__head">
      <h2>POS 日結異常稽核軌跡（近 {{ days }} 天）</h2>
      <el-select v-model="days" @change="load" size="small">
        <el-option :value="7" label="近 7 天" />
        <el-option :value="30" label="近 30 天" />
        <el-option :value="90" label="近 90 天" />
        <el-option :value="180" label="近 180 天" />
      </el-select>
    </div>

    <el-empty v-if="!loading && events.length === 0" description="此區間無解鎖事件" />

    <el-timeline v-else>
      <el-timeline-item
        v-for="ev in events"
        :key="ev.id"
        :timestamp="ev.occurred_at"
        :type="ev.action === 'admin_override' ? 'danger' : 'warning'"
      >
        <div class="pos-audit__event">
          <strong>
            {{ ev.action === 'admin_override' ? '🔓 Admin Override 解鎖' : '🔓 解鎖' }}
            — {{ ev.close_date }}
          </strong>
          <div>解鎖人：{{ ev.unlocker_username }}（{{ ev.unlocker_role }}）</div>
          <div class="pos-audit__comment">{{ ev.comment }}</div>
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
```

### 2.5 路由與入口

- 在 `src/router/index.js` 加入 route `/activity/audit/pos-unlock` → `POSAuditEventsView.vue`
  - route meta 帶 `requiresPermission: 'ACTIVITY_PAYMENT_APPROVE'`（沿用既有 router guard 模式）
- 在 `POSApprovalView.vue` 加入連結按鈕：「異常稽核軌跡」→ `router.push('/activity/audit/pos-unlock')`
  - 按鈕本身用 `v-if="hasPermission('ACTIVITY_PAYMENT_APPROVE')"` 條件渲染（避免無權限者誤點後 403）

---

## 3. DB / Migration

**無 migration**：
- `ApprovalLog.action` 是 `String(50)`，新增字面值 `admin_override` 不需 schema 變更
- `User.line_user_id` / `line_follow_confirmed_at` 已存在
- `ActivityPosDailyClose` 不變

---

## 4. 測試

新檔 `tests/test_pos_unlock_separation.py`：

| # | 測試 | 期望 |
|---|------|------|
| 1 | `test_unlock_by_original_approver_rejected_403` | 原簽核人 unlock 自己的 → 403，detail 含「原簽核人」 |
| 2 | `test_unlock_by_other_approver_succeeds` | 不同 PAYMENT_APPROVE 持有者 unlock → 200 |
| 3 | `test_admin_override_with_long_reason_succeeds` | role='admin' + override + reason 30 字 → 200，ApprovalLog `action='admin_override'` |
| 4 | `test_admin_override_short_reason_rejected_422` | role='admin' + override + reason < 30 字 → 422 |
| 5 | `test_non_admin_with_override_flag_rejected_403` | 一般 PAYMENT_APPROVE + `is_admin_override=true` → 403 |
| 6 | `test_unlock_response_notification_delivered_false_when_no_line_binding` | 原簽核人 `line_user_id IS NULL` 或 `line_follow_confirmed_at IS NULL` → response `notification_delivered=false`，unlock 仍成功 200 |
| 7 | `test_approve_warnings_when_approver_is_today_operator` | approver = 當日 operator → response `warnings` 非空 |
| 8 | `test_approve_no_warnings_when_approver_did_not_operate_today` | approver ≠ 當日 operator → `warnings=[]` |
| 9 | `test_audit_endpoint_returns_recent_unlock_events_only` | 只回傳 doc_type=`activity_pos_daily` 且 action 在 `cancelled / admin_override` 集合 |
| 10 | `test_audit_endpoint_orders_desc_and_limits_200` | 構造 250 筆，確認回傳 200 筆按時間倒序 |

修舊測試：
- `test_activity_pos.py::TestPosDailyClose::test_unlock_*`：現有解鎖測試的 reason 字數可能 < 30（admin override path 不適用，但若用了「原簽核人」帳號解鎖會被新守衛擋下）；逐一檢查並修

---

## 5. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 兩個簽核者其中一人離職，另一人卡住 | role='admin'（通常 = 老闆）可 override；長期應補入第三人 |
| Admin override 被濫用 | reason ≥ 30 字 + LINE 通知原簽核人 + dashboard 公開可查 |
| 原簽核人無 LINE 綁定 → 通知失敗 | response `notification_delivered=false` 提示解鎖人私下告知；員工 LINE 綁定屬後續工作 |
| `_push_to_user` 例外導致 unlock commit 卡住 | best-effort try/except 包住，commit 後執行 |
| 解鎖前端 prompt 字數驗證與後端不一致 | 前端 minLen 與後端 `_*_REASON_MIN_LENGTH` 同源 const；plan 階段以 grep 確認對齊 |
| `_serialize_close` 多端點共用，不可加 warnings | warnings 只在 POST handler inline append，不污染共用 helper |
| API contract 從 204 → 200 + JSON | 前端 unlockPOSDailyClose 既有呼叫處全面檢查 |

---

## 6. 實作順序

1. **後端 schema + 守衛**：`DailyCloseUnlock` 新欄位、4-eye 守衛、ApprovalLog action 區分（含測試 1-5）
2. **後端 LINE 通知**：`line_service.notify_pos_unlock_to_approver()` + best-effort try/except（含測試 6）
3. **後端 approve warnings**：`approve_daily_close` warnings 欄位（含測試 7-8）
4. **後端 audit endpoint**：`/activity/audit/pos-unlock-events`（含測試 9-10）
5. **後端 final commit**
6. **前端 API 模組**：`unlockPOSDailyClose` payload 變、新增 `getPOSUnlockEvents`
7. **前端 POSApprovalView**：unlock 三分支 + approve warnings
8. **前端新 view**：`POSAuditEventsView.vue` + 路由 + 入口連結
9. **前端 final commit**
10. **整合驗證**：3 個 golden path（一般 4-eye / admin override / approve warning）

每步可獨立 commit；後端與前端各一筆 commit（per workspace 慣例）。

---

## 7. Out of scope（明確排除）

- approve 4-eye（明確選 A 方案；風險已用 warnings 軟提醒覆蓋）
- 通用 ApprovalLog 全表 UI（dashboard 限 doc_type='activity_pos_daily' + 限 unlock 類 action）
- 員工 LINE 綁定流程（既有 `User.line_user_id` 欄位；綁定流程屬後續單獨工作）
- 簽核冷卻期 / 5 分鐘 retract window（Q2 D 選項未選）
- C3/C4/C5 + H 系列其他稽核 finding
