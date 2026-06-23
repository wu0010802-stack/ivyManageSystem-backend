# 才藝報名狀態機（Enum + 合法轉移表）設計 spec

- 日期：2026-06-23
- 範圍：後端為主（`RegistrationCourse.status`、`ActivityRegistration.match_status`）。前端**零 wire 變更**（Enum 值即現有字串）。
- 緣起：2026-06-22 才藝「統一狀態機 / 金流口徑」檢視 finding #1。本 spec 是該檢視結論第 4 波的設計文件。
- 前置脈絡：A/B/C + 第2波（upcomingCount / is_paid 篩選統一）+ 第3波（講師 / 下次上課）已落地（見 `2026-06-22-...` 系列 memory）。狀態機是檢視真正的核心改善，但屬最大跨檔重構，故獨立 spec。

---

## 1. 問題（檢視 finding 摘要）

才藝模組的狀態欄位**全是 `Column(String(20))` 裸字串**，狀態值散落在各 handler 直接賦值，**無 Enum、無 SQLEnum、無合法轉移表**。對照之下：

- `services/student_lifecycle.py:41` 有正規 state machine（`ALLOWED_TRANSITIONS` + `transition()` + `is_transition_allowed()`），但 activity **完全沒用**。
- `services/recruitment_funnel.py` 也有 `can_transition` / `transition_visit`。

唯一集中常數是 `services/activity_service.py:30 OCCUPYING_STATUSES = ("enrolled","promoted_pending")`（只涵蓋「佔位態」，非完整集合、非轉移表）。

**後果**：任何 handler 可任意覆寫 status，無前置合法性檢查；typo / 非法轉移（例：把 `rejected` 直接改回 `enrolled` 而漏掉容量重檢）只能靠人工 review 擋；狀態語意散落、難稽核。

---

## 2. 目標 / 非目標

### 目標
1. 為 `RegistrationCourse.status` 與 `ActivityRegistration.match_status` 各建一個 **`str` Enum**（值 = 現有字串，wire 不變）。
2. 各建一張 **合法轉移表**（`ALLOWED_TRANSITIONS`）+ **集中 transition 服務**（validate → 更新 → 寫稽核 log），對齊 `student_lifecycle.transition` 慣例。
3. 所有狀態寫入點改走 transition 服務；非法轉移 raise → HTTP 400。
4. **不改 wire / 不改前端**：Enum 值即現有字串；OpenAPI response 仍是同樣字串（可選擇是否把 `Literal[...]` 收進 schema 讓前端 codegen 拿到 union，見 §7）。

### 非目標
- **不**處理 `payment_status`（那是 read-time 衍生值，非儲存狀態機；第2波已統一其篩選真相來源）。
- **不**改 `ActivityRegistration` 的「報名生命週期」（目前由 `is_active` + `pending_review` + `match_status` 拼湊；本波只把 `match_status` 形式化，報名整體生命週期是否要獨立 status 欄留待後續）。
- **不**動 student lifecycle / recruitment funnel（已是 state machine）。
- **不**改容量 / 金流計算邏輯（transition 只管狀態合法性 + 稽核；副作用如重算 is_paid、清計時欄位仍由 caller 負責，見 §5 風險）。

---

## 3. 現況盤點（狀態值 + 寫入點，來自檢視）

### 3.1 `RegistrationCourse.status`（`models/activity.py:241`，default `enrolled`）
有效值集合（實際寫入 DB）：`{enrolled, waitlist, promoted_pending}`。
> 註：`cancelled` 只出現在 `pos_approval.py` / `dashboard_query_service.py` 的查詢比對，**無寫入點**，疑為跨域殘留（spec 階段 0 須確認是否 dead code，若確認則 Enum 不含它）。`confirmed` 是 `_shared.py:485` 的 `review_state` UI hint，非 course status。

寫入點（賦值 `.status = ...`）：
- `services/activity_service.py:801`、`:853` → `enrolled`（候補確認 / 退課補位）
- `services/activity_service.py:1287` → `promoted_pending`（候補升正式，設 `promoted_at` / `confirm_deadline`）
- `api/activity/registrations_pending.py:863` → `waitlist`
- 報名建立：`api/activity/public.py`（公開報名）、`api/parent_portal/activity.py:316/318`（家長端，enrolled/waitlist）、`registrations_items.py`（後台加課）→ 初始 `enrolled` / `waitlist`
- 候補轉正 orchestration：`api/activity/public.py public_confirm_promotion` → `activity_service.confirm_waitlist_promotion`
- restore / 過期 sweep：`registrations_pending.restore_registration`、`activity_service` 的 sweep（`:923/994/1063` 查 promoted_pending）

### 3.2 `ActivityRegistration.match_status`（`models/activity.py:162`，default `unmatched`，有 index）
有效值：`{unmatched, matched, pending, rejected, manual}`（語意註解見 model `:161`）。

寫入點：
- `api/activity/public.py:726` → `matched if is_matched else pending`（公開報名比對）
- `api/activity/public.py:1031` → `matched`
- `api/activity/registrations_pending.py:399` → `manual`（人工綁定）
- `api/activity/registrations_pending.py:453` → `rejected`（駁回）
- `api/activity/registrations_pending.py:596` → `matched`（rematch 成功）
- `api/activity/registrations_pending.py:818` → `pending`（restore）

---

## 4. 提案設計（對齊 `student_lifecycle` 慣例）

### 4.1 Enum + 常數（`models/activity.py` 或新 `models/activity_status.py`）
```python
import enum

class RegistrationCourseStatus(str, enum.Enum):
    ENROLLED = "enrolled"
    WAITLIST = "waitlist"
    PROMOTED_PENDING = "promoted_pending"

class MatchStatus(str, enum.Enum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PENDING = "pending"
    REJECTED = "rejected"
    MANUAL = "manual"
```
- **不改 column 型別**（維持 `String(20)`，**不**用 SQLEnum）：避免 DB enum 的 migration 脆弱性（新增值要 ALTER TYPE）、且與既有 `permtxt01`「權限改字串集合」的去-enum 方向一致。Enum 僅作**應用層**真相來源（值即字串）。
- `OCCUPYING_STATUSES` 改以 Enum 表示：`frozenset({RegistrationCourseStatus.ENROLLED, RegistrationCourseStatus.PROMOTED_PENDING})`。

### 4.2 合法轉移表 + transition 服務（新 `services/activity_status.py`）
比照 `student_lifecycle`：`ALLOWED_TRANSITIONS: dict[Status, set[Status]]` + 純函式 `is_transition_allowed` + `transition_*()`。

**`RegistrationCourse.status` 提案轉移表（⚠ 邊界須在階段 0 對 sweep/restore/withdraw 逐一核實）：**
```
waitlist          → {promoted_pending}          # promote_waitlist 開缺
promoted_pending  → {enrolled, waitlist}         # enrolled=家長確認；waitlist=過期釋出退回候補(待核實)
enrolled          → {}                           # 退課=刪 RC 列(非狀態轉移)；補位 waitlist→enrolled 走上面
```
初始狀態（建立報名）：`enrolled` 或 `waitlist`（非轉移，走 builder，不經 transition）。

**`match_status` 提案轉移表：**
```
unmatched → {matched, pending}        # 公開報名比對結果
pending   → {matched, manual, rejected}
matched   → {pending, manual, rejected}   # rematch 可再變(待核實是否允許)
manual    → {pending, rejected}            # (待核實)
rejected  → {pending}                      # restore
```

**transition 服務簽名（對齊 student_lifecycle）：**
```python
class ActivityTransitionError(ValueError): ...

def transition_registration_course_status(
    session, rc: RegistrationCourse, to: RegistrationCourseStatus,
    *, operator: str, reason: str | None = None,
) -> None:
    cur = RegistrationCourseStatus(rc.status)
    if to not in ALLOWED_RC_TRANSITIONS.get(cur, set()):
        raise ActivityTransitionError(f"不允許的選課狀態轉移：{cur.value} → {to.value}")
    rc.status = to.value
    activity_service.log_change(session, rc.registration_id, ..., "狀態轉移", ..., operator)
    # caller 仍負責副作用（重算 is_paid / 清計時欄 / 容量重檢）
```
- 非法轉移 → `ActivityTransitionError`（`ValueError` 子類）→ 既有 exception handler 轉 HTTP 400（對齊 `LifecycleTransitionError`）。
- **稽核**：每次轉移寫 `log_change`（才藝既有稽核機制），與 student_lifecycle 寫 `StudentChangeLog` 對等。

### 4.3 副作用歸屬（關鍵設計界線）
transition 服務**只管狀態合法性 + 稽核**，**不**內含業務副作用，因為各轉移副作用差異大且已散落既有 handler：
- `waitlist → promoted_pending`：設 `promoted_at` / `confirm_deadline` / 清提醒旗標（caller）。
- `promoted_pending → enrolled`：重算 `is_paid`（total 上升）+ 容量已在 promote 時佔用（caller）。
- `promoted_pending → waitlist`（過期）：清三計時欄 None（caller；對齊 2026-06-22 bughunt 修補）。

→ transition 服務提供「合法性閘 + 稽核」，副作用維持在 caller。這比把所有副作用塞進 transition 更安全、改動面更小（降低與平行 churn 衝突）。

---

## 5. 風險

1. **與平行 session 高度衝突**：本 session 每次合併都被平行 activity session hijack（main 被推進 7+ 次）。狀態寫入點橫跨 `public.py` / `registrations_pending.py` / `activity_service.py` / `parent_portal` —— 正是平行最常改的檔。**big-bang 一次改完所有寫入點會大面積 rebase 衝突**。→ 見 §6 分階段。
2. **轉移表邊界判錯會擋掉合法流程**：例如過期 `promoted_pending` 的去向、rematch 對 `matched`/`manual` 的再轉移。判太嚴 → 正常操作 400。→ 階段 1 先「soft-enforce」（log warning 不 raise）觀察。
3. **副作用漏接**：若把寫入點改走 transition 卻漏掉原本緊跟的副作用（清計時欄 / 重算 is_paid），會重現 bug。→ 每個寫入點遷移時逐一比對原副作用（TDD 守護）。
4. **初始狀態 vs 轉移**：建立報名的初始 `enrolled`/`waitlist` 不是「轉移」（無 from），勿強迫走 transition（會找不到 from）。builder 路徑與 transition 路徑分開。

---

## 6. 分階段 rollout（為在平行 churn 中安全落地）

**核心策略：小步、可獨立合併、每步零行為變更或可回退，降低與平行 session 的衝突面。**

- **階段 0（純新增，零行為變更，先合併卡位）**：新增 `RegistrationCourseStatus` / `MatchStatus` Enum + `ALLOWED_TRANSITIONS` 表 + `is_transition_allowed` 純函式 + `transition_*` 服務 + 完整單元測試（轉移表正確性）。**不改任何 handler**。順帶確認 `cancelled` 是否 dead code。→ 一個小 PR，幾乎不會與平行衝突。
- **階段 1（逐寫入點遷移，soft-enforce）**：`transition_*` 加 `enforce: bool = False`；非法轉移時 `enforce=False` 只 `logger.warning` 不 raise。逐一把寫入點改成 `transition_*(...)`（一次一個檔 / 一組，獨立 commit），每個都 TDD 守護原副作用不變。觀察 log 一段時間確認轉移表無誤判。
- **階段 2（enforce）**：確認 log 無非預期 warning 後，`enforce=True`（非法轉移 raise 400）。
- **階段 3（收斂）**：移除散落的裸字串賦值殘留、`OCCUPYING_STATUSES` 改 Enum、文件化。

> 每階段都是可獨立合併的小批，與本 session 的「隔離 worktree off 最新 main → rebase → 守衛 --no-ff」流程相容。**建議在 activity 平行 review/bughunt session 收斂後再啟動階段 1**（階段 0 可隨時做）。

---

## 7. 前端 / OpenAPI 影響

- **wire 零變更**：Enum 值 = 現有字串，response 仍回同樣字串。
- 可選增強：把 response_model 的 status 欄改 `Literal["enrolled","waitlist","promoted_pending"]` / match_status 同理 → 前端 codegen（C 波已接）會拿到 union 型別而非 `str`，IDE 收斂。此為**可選**，會造成一次 schema.d.ts 漂移（需 gen:api），建議併入未來 schema reconcile 批，不在階段 0 強制。

---

## 8. 測試策略

- **階段 0**：`tests/test_activity_status_machine.py` —— 轉移表正確性（每條合法 edge 通過、非法 edge 被擋）、Enum 值 == 字串、`is_transition_allowed` 純函式、終態空集合。
- **階段 1**：每個遷移的寫入點，先寫「原副作用不變」的守護測試（red→green 確保遷移無行為變更），再改走 transition。
- **回歸**：`test_activity_*`（pos / fee / pending / parent）全綠；候補轉正 / 駁回 / restore / 過期 sweep 的既有測試是關鍵守護。

---

## 9. Open questions（階段 0 須先釐清）
1. `cancelled` 是否 dead code？（決定 `RegistrationCourseStatus` 是否含它）
2. 過期 `promoted_pending` 的去向：退回 `waitlist`？刪 RC？保持並由下輪 sweep 處理？（決定 RC 轉移表該 edge）
3. `match_status` 的 `matched`/`manual` 是否允許被 rematch 再轉移？（決定 match 轉移表該 edge）
4. 是否要把「報名整體生命週期」（`is_active` + `pending_review`）也形式化為單一 status 欄？（本波非目標，但影響長期設計）

---

## 10. 工時估（粗估）
- 階段 0：0.5 天（Enum + 表 + 服務 + 單元測試）。
- 階段 1：1–2 天（~10 個寫入點逐一遷移 + 守護測試，視平行衝突而定）。
- 階段 2–3：0.5 天。
- 合計 ~2–3 天，**強烈建議分多個小 PR**、避開 activity 平行高峰。
