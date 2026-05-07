# POS 操作員活動稽核 Dashboard（H1）設計

**日期**：2026-05-07
**狀態**：📝 Draft
**前置稽核**：2026-05-06 對話內 POS 企業稽核 H1 項
**業務決策**：採營運政策方案 C「每位員工各自登入個人帳號」（既有，純文化/紀律問題）
**範圍**：跨前後端 + 1 份 SOP 文件

---

## 0. 範圍與目標

### 問題

POS 收銀寫入的 `ActivityPaymentRecord.operator` 來自 `current_user.username`。若員工共用 `cashier01`/`admin` 帳號，多人交易全標同一帳號 → 無法歸責。

業主確認：每位員工**已有個人帳號**，問題在於老師圖方便共用。技術介入有限：給老闆一個 dashboard 看「最近 N 天每位帳號的 POS 操作筆數」，搭配書面政策強制大家用個人帳號。

### 範圍

✅ **包含**：
- 後端 `GET /api/activity/audit/operator-activity?days=30` 端點
- 前端 `POSAuditEventsView` 加第二個 tab「操作員活動」
- SOP 文件 `docs/sop/pos-operator-policy.md`（workspace 層級）
- 新測試 `tests/test_pos_operator_activity.py`

❌ **不包含**：
- PIN 程式碼（因業主選擇政策路線而非技術路線）
- 共用 vs 個人帳號的 model-level 標記（無實益）
- 批次建立員工帳號（題意確認個人帳號已存在）
- 異常自動偵測（紅燈 > 50 筆/天等）— 老闆肉眼判斷即可

### 成功標準

1. `GET /api/activity/audit/operator-activity?days=30` 回傳近 30 天有 POS 紀錄的所有 operator，含 payment / refund 筆數、最後操作時間、JOIN User 後的 display_name / role
2. 權限：`ACTIVITY_PAYMENT_APPROVE`（與 `pos-unlock-events` 一致）
3. 結果排序：總筆數（payment + refund）倒序，限 100 筆
4. 「無對應 User row」的 operator（例：已停用帳號殘留）以特別標記回傳，前端紅色顯示
5. 前端在既有 `POSAuditEventsView` 用 `<el-tabs>` 切兩 tab：解鎖事件 / 操作員活動
6. SOP 文件記錄「每位員工須以個人帳號登入；共用帳號將由老闆每月稽核 dashboard 抓出」

---

## 1. 後端

### 1.1 新端點 `api/activity/pos_approval.py`

放在既有 `list_pos_unlock_events` 旁，沿用同模組（避免新增 router 檔）。

```python
@router.get("/activity/audit/operator-activity")
async def list_operator_activity(
    days: int = Query(30, ge=1, le=180, description="查詢過去 N 天"),
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """列出近 N 天每位 POS operator 的活動量。

    Why (spec H1): 業主政策要求每位員工以個人帳號操作 POS；本端點供老闆每月
    稽核哪些帳號操作量異常（共用帳號通常筆數高、個人帳號零）以揪出違規。

    JOIN User 表豐富 display_name / role / employee_id；無 User row 對應的
    operator 字串以 user=None 回傳，前端以紅色標記提醒（已停用 / 共用殘留）。
    """
    cutoff_date = (datetime.now(TAIPEI_TZ).replace(tzinfo=None) - timedelta(days=days)).date()
    session = get_session()
    try:
        # 用 SQL 聚合 payment / refund 筆數 + 最後活動時間
        rows = (
            session.query(
                ActivityPaymentRecord.operator,
                func.sum(
                    case((ActivityPaymentRecord.type == "payment", 1), else_=0)
                ).label("payment_count"),
                func.sum(
                    case((ActivityPaymentRecord.type == "refund", 1), else_=0)
                ).label("refund_count"),
                func.max(ActivityPaymentRecord.created_at).label("last_at"),
            )
            .filter(
                ActivityPaymentRecord.payment_date >= cutoff_date,
                ActivityPaymentRecord.voided_at.is_(None),
                ActivityPaymentRecord.operator.isnot(None),
                ActivityPaymentRecord.operator != "",
            )
            .group_by(ActivityPaymentRecord.operator)
            .order_by(
                (
                    func.sum(case((ActivityPaymentRecord.type == "payment", 1), else_=0))
                    + func.sum(case((ActivityPaymentRecord.type == "refund", 1), else_=0))
                ).desc()
            )
            .limit(100)
            .all()
        )

        if not rows:
            return {"days": days, "count": 0, "operators": []}

        # 一次 query 拉所有對應 User
        from models.auth import User
        usernames = [r.operator for r in rows]
        users = (
            session.query(User)
            .filter(User.username.in_(usernames))
            .all()
        )
        user_by_name = {u.username: u for u in users}

        operators = []
        for r in rows:
            u = user_by_name.get(r.operator)
            operators.append({
                "operator": r.operator,
                "payment_count": int(r.payment_count or 0),
                "refund_count": int(r.refund_count or 0),
                "total_count": int((r.payment_count or 0) + (r.refund_count or 0)),
                "last_activity_at": (
                    r.last_at.isoformat(timespec="seconds") if r.last_at else None
                ),
                "user": (
                    {
                        "id": u.id,
                        "display_name": u.display_name or u.username,
                        "role": u.role,
                        "employee_id": u.employee_id,
                        "is_active": bool(u.is_active),
                    }
                    if u
                    else None
                ),
            })
        return {"days": days, "count": len(operators), "operators": operators}
    finally:
        session.close()
```

> **import 補充**：在 file 頂端 `from sqlalchemy import case, func`（func 已有）。

### 1.2 新測試 `tests/test_pos_operator_activity.py`

| # | 測試 | 期望 |
|---|------|------|
| 1 | `test_returns_operators_with_counts` | 收 2 筆（A）+ 退 1 筆（B），endpoint 回 2 個 operator，payment/refund 數正確 |
| 2 | `test_orders_by_total_count_desc` | A 收 5 筆、B 收 1 筆 → A 排第一 |
| 3 | `test_excludes_voided_records` | 收 3 筆其中 1 筆 voided → 該 operator 應只計 2 筆 payment |
| 4 | `test_user_field_null_when_no_account` | operator 沒對應 User → response 該筆 `user=null`（不擋 endpoint） |
| 5 | `test_user_field_includes_display_name_role` | operator 對應 User → response 含 display_name / role / employee_id |
| 6 | `test_permission_guard_403_without_approve` | 一般 ACTIVITY_READ user → 403 |
| 7 | `test_days_query_limits_window` | 早於 cutoff 的紀錄不計入 |

---

## 2. 前端

### 2.1 改造 `src/views/activity/POSAuditEventsView.vue`

拆兩個 tab。既有解鎖事件改稱 tab 1「解鎖事件」；新增 tab 2「操作員活動」。

```vue
<template>
  <div class="pos-audit">
    <el-card>
      <template #header>
        <h2 class="pos-audit__title">POS 異常稽核軌跡</h2>
      </template>

      <el-tabs v-model="activeTab">
        <el-tab-pane label="解鎖事件" name="unlock">
          <!-- 既有解鎖事件 timeline 內容搬到這裡 -->
        </el-tab-pane>
        <el-tab-pane label="操作員活動" name="operator">
          <OperatorActivityTab :days="days" @days-change="days = $event" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>
```

### 2.2 新元件 `src/components/activity/OperatorActivityTab.vue`

```vue
<template>
  <div class="operator-activity">
    <div class="operator-activity__head">
      <el-select v-model="localDays" size="small" style="width: 140px" @change="reload">
        <el-option :value="7" label="近 7 天" />
        <el-option :value="30" label="近 30 天" />
        <el-option :value="90" label="近 90 天" />
        <el-option :value="180" label="近 180 天" />
      </el-select>
      <span class="operator-activity__hint">
        💡 個人帳號 POS 操作量低、共用帳號異常高？請落實「個人帳號登入」政策
      </span>
    </div>

    <el-empty v-if="!loading && rows.length === 0" description="此區間無 POS 操作紀錄" />

    <el-table v-else :data="rows" size="small" :max-height="500">
      <el-table-column label="帳號" prop="operator" min-width="120">
        <template #default="{ row }">
          <code>{{ row.operator }}</code>
          <el-tag v-if="!row.user" type="danger" size="small" effect="plain" style="margin-left: 6px">
            無 User row
          </el-tag>
          <el-tag v-else-if="row.user && !row.user.is_active" type="warning" size="small" effect="plain" style="margin-left: 6px">
            帳號停用
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="顯示名" min-width="120">
        <template #default="{ row }">{{ row.user?.display_name || '—' }}</template>
      </el-table-column>
      <el-table-column label="Role" width="100">
        <template #default="{ row }">{{ row.user?.role || '—' }}</template>
      </el-table-column>
      <el-table-column label="收款筆數" prop="payment_count" width="100" align="right" />
      <el-table-column label="退費筆數" prop="refund_count" width="100" align="right" />
      <el-table-column label="總筆數" width="100" align="right">
        <template #default="{ row }"><strong>{{ row.total_count }}</strong></template>
      </el-table-column>
      <el-table-column label="最後操作時間" min-width="160">
        <template #default="{ row }">{{ row.last_activity_at }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getPOSOperatorActivity } from '@/api/activity'

const props = defineProps({
  days: { type: Number, default: 30 },
})
const emit = defineEmits(['days-change'])

const localDays = ref(props.days)
const rows = ref([])
const loading = ref(false)

watch(localDays, (v) => emit('days-change', v))

async function reload() {
  loading.value = true
  try {
    const { data } = await getPOSOperatorActivity(localDays.value)
    rows.value = data.operators || []
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '載入失敗')
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.operator-activity__head {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 12px;
}
.operator-activity__hint {
  font-size: 13px;
  color: #64748b;
}
</style>
```

### 2.3 新 API 模組

在 `src/api/activity.js` 加：
```javascript
export const getPOSOperatorActivity = (days = 30) =>
  api.get('/activity/audit/operator-activity', { params: { days } })
```

---

## 3. SOP 文件

### 3.1 新檔 `ivyManageSystem/docs/sop/pos-operator-policy.md`

> 注意：放在 **workspace 根目錄** 而非後端 / 前端 repo，因為這是跨團隊政策文件

```markdown
# POS 操作員帳號使用規範

**生效日期**：2026-05-07
**對應稽核**：H1（POS operator 歸責）

## 規定

1. **每位員工須以個人 username 登入** POS 進行收銀 / 退費操作
2. **嚴禁共用帳號**（如 `admin`、`cashier01` 等共用帳號不得用於日常 POS 操作）
3. 主管帳號（`admin`）僅供「設定管理」與「日結簽核」使用，不得用於日常收款

## 違規後果

- 老闆每月（建議每月 1 日）查看 `/activity/audit/pos-unlock` → 「操作員活動」tab
- 若發現：
  - 共用帳號（如 `cashier01`）筆數異常高 → 列為違規
  - 個人帳號筆數為零 → 該員工未落實規範
- 連續 2 個月違規 → 影響年終評核

## 帳號管理

- 新進員工：HR 於入職首日請 admin 建立個人 User 帳號（`/admin/employees`）並交付**初始密碼**
- 員工離職：admin 應立即停用該帳號（`is_active=false`）
- **嚴禁** admin 把自己的密碼告訴老師
```

---

## 4. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 老闆從不看 dashboard → 規範形同虛設 | SOP 寫明每月 1 日例行檢視，配合既有「異常稽核軌跡」入口連結 |
| operator 字串可能被 LIKE 注入篩選 | 本端點不接受 operator 過濾參數；只支援 days 整數參數 |
| 大量 operator（> 100）時資料截斷 | 排序倒序 + limit 100 已確保看到 top 操作量；極端情況加分頁屬於 follow-up |
| 政策文件被遺忘 | 記入 workspace `CLAUDE.md` cross-cutting 段以便 Claude 將來提醒 |

---

## 5. 實作順序

1. 後端 endpoint + 7 個測試 → backend commit
2. 前端 API 模組 + OperatorActivityTab 元件 + POSAuditEventsView tab 化 → frontend commit
3. SOP 文件 + workspace CLAUDE.md 加 cross-cutting note → 隨前後端任一 commit 帶上（建議後端 commit）
4. 整合驗證：dev 環境登入 admin → 看 dashboard → 確認 tab 切換 + 表格正常

---

## 6. Out of scope（明確排除）

- 任何 PIN 程式碼（業主政策路線）
- 共用 vs 個人帳號 model-level 標記
- 自動異常偵測 / 告警（老闆每月手動看就夠）
- 員工個人帳號批次建立工具（既有個人帳號已存在）
- 強制密碼複雜度提升 / 雙因素驗證（屬另案）
