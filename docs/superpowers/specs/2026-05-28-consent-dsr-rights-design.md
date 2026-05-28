# Consent Infrastructure + DSR 五權利完成（P0c）

**日期**: 2026-05-28
**範圍**: ivy-backend + ivy-frontend（含 parent 端 + portal 員工端）
**Sprint**: P0c（4 個 P0 法規/個資 sprint 中的第三個，規模最大）
**預估**: ~2 工作週

---

## 1. 背景與動機

### 1.1 Consent 機制缺席（個資法 §8）

`src/parent/views/LoginView.vue:1-92` LIFF 進來只 init→liffLogin→setUser→/home，無同意書 modal、無連結到隱私權政策；`BindView.vue:44-95` 綁定碼頁僅一行 legal 文字。grep 全 repo 無 `parent_consent_log` / `policy_version` 表。

**違反**：個資法 §8（蒐集時應告知）、§19（特定目的必要範圍）、兒少法 §46（散布兒少照片同意）、GDPR Art. 7（demonstrable consent）。

**風險**：每事件可罰 2-20 萬，連續處罰；無法舉證家長曾同意 → 任一家長申訴即敗訴。

### 1.2 DSR 五權利狀態（個資法 §3）

| 權利 | 法源 | 家長端現況 | 員工端現況 |
|------|------|---------|---------|
| 查詢/副本 | §3.1 | ✅ `GET /api/parent/me/data-export` 已存在 | ❌ 無 |
| 補充更正 | §3.2 | ⚠️ ChildProfileView 可改部分欄位但無 audit-trail 化「更正申請」流程 | ❌ 無 |
| 停止處理利用 | §3.3 | ❌ 無 opt-out 機制 | ❌ 無 |
| 查詢同意紀錄 | §3.3 衍生 | ❌ 無（依賴 1.1 的 consent log） | N/A |
| 刪除 | §3.5 | ❌ 無 self-service delete request | ❌ 無 |

**狀態**：家長端 **1.5/5**（export 完成、correct 半成），員工端 **0/5**。

---

## 2. 目標與非目標

### 目標

**Consent (P0c-1)**:
1. 建 `parent_consent_log` 表記錄家長同意事件（scope-aware）
2. 建 `policy_versions` 表追蹤隱私權政策版本
3. LIFF LoginView 加同意書 modal，新使用者強制同意 + 既有使用者 policy bump 強制重簽
4. Scope 分類：`service_essential`（服務必要）/ `photo_publish`（照片公開）/ `line_push`（LINE 推播）/ `cross_border_transfer`（跨境傳輸如 Supabase US region）

**DSR (P0c-2)**:
5. 家長端：補 delete request flow（`POST /api/parent/me/delete-request`）+ opt-out（`POST /api/parent/me/opt-out`）+ 同意紀錄查詢（`GET /api/parent/me/consents`）+ correct request（`POST /api/parent/me/correct-request`，現有可改欄位流程化為 audit-trail）
6. 員工端：補 `GET /api/portal/my-data-export`（員工自身完整資料下載，含薪資歷史）

### 非目標
1. **既有家長帳號 backfill「假定同意」**：法律不允許追溯同意，policy version bump 時必走重簽流程。Cutover day 起家長 LIFF 進入會被攔到 modal，3 天內未同意則無法繼續用 portal（acceptable downtime risk）。
2. **DSR delete 即時硬刪**：刪除是 request → admin review（72hr 內）→ 走 student_lifecycle GC（既有 365d PII retention 機制）。家長端立刻硬刪不可行（學生資料涉合班/出席/費用稽核需保留稅務 7 年）。
3. **員工 DSR 完整流程**：員工資料受勞基法 §80 「員工資料保存 5 年」約束，delete request 流程 v1 不做（next sprint），v1 只做 export。
4. **同意撤回的下游 cascade**：撤回 `photo_publish` 不會自動硬刪歷史照片，僅未來不再可被廣播；過去已下載到家長手機的照片無法收回。

---

## 3. 設計

### 3.1 Schema (Alembic migration)

```python
# parent_consent_log: 每筆 = 一次同意事件
class ParentConsentLog(Base):
    __tablename__ = "parent_consent_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    policy_version_id = Column(Integer, ForeignKey("policy_versions.id"), nullable=False)
    scope = Column(String(50), nullable=False)  # service_essential / photo_publish / line_push / cross_border_transfer
    consented = Column(Boolean, nullable=False)  # true=同意, false=撤回（同樣寫入 log）
    consented_at = Column(DateTime, nullable=False, default=now_taipei_naive)
    ip_address = Column(String(45), nullable=True)  # IPv6 max length
    user_agent = Column(Text, nullable=True)
    note = Column(Text, nullable=True)  # 撤回理由、特殊情境記載

    __table_args__ = (
        Index("ix_pcl_user_scope_time", "user_id", "scope", "consented_at"),
    )


# policy_versions: 政策本身的版本管控
class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id = Column(Integer, primary_key=True)
    version = Column(String(20), nullable=False, unique=True)  # e.g., "2026.1"
    effective_at = Column(DateTime, nullable=False)
    document_path = Column(String(255), nullable=False)  # 指向 storage 上的 PDF/HTML
    summary = Column(Text, nullable=True)  # 中文版本說明
    created_at = Column(DateTime, default=now_taipei_naive)
```

### 3.2 後端 endpoints (新檔 `api/parent_portal/consent.py` + `api/parent_portal/dsr.py`)

**Consent**:
- `GET /api/parent/me/consents` — 列出當前家長對各 scope 的最新同意狀態 + history
- `POST /api/parent/me/consent` — 寫入新同意（body: `{policy_version: str, scope: str, consented: bool}`）
- `GET /api/policies/current` — 公開（無需 auth），回當前生效 policy_version + document URL

**DSR**:
- `POST /api/parent/me/delete-request` — 建立刪除請求 → 通知 admin → admin review queue
- `POST /api/parent/me/opt-out` — 停止特定處理（scope 撤回 = 寫 consent_log consented=false）
- `POST /api/parent/me/correct-request` — 提交資料更正請求（含 field/new_value/reason）→ admin review

**Employee DSR**:
- `GET /api/portal/my-data-export` — 員工自身資料（含 employee profile、薪資歷史、考勤、請假、考核）

**Admin queue**:
- `GET /api/admin/dsr-requests` — 列出待處理刪除/更正請求
- `POST /api/admin/dsr-requests/{id}/approve` / `/reject`

### 3.3 前端 (ivy-frontend)

**Parent LIFF**:
- `src/parent/views/LoginView.vue` 加 consent modal：首次登入或 policy 升版時擋下；用 element-plus dialog；4 個 scope 勾選；連結到 `/policies/current`
- `src/parent/views/MeView.vue` 加「個資權利」section：
  - 「下載我的資料」（連動既有 `/me/data-export`）
  - 「申請刪除」按鈕 → 確認 dialog → `POST /me/delete-request`
  - 「申請更正」表單 → 選欄位 + 新值 + 理由
  - 「查詢同意紀錄」→ 列出 scope 表格 + 撤回按鈕
- `src/parent/views/BindView.vue` 不擋（已綁碼者代表已簽舊版同意）

**Admin**:
- 新增 `src/views/DsrRequestsView.vue` 處理刪除/更正 queue
- `src/views/PolicyVersionsView.vue` 管理政策版本（上傳新版本 → 觸發所有 user 重簽）

**Employee Portal**:
- `src/portal/views/MyDataView.vue` 加「下載我的資料」按鈕（呼叫 `/portal/my-data-export`）

### 3.4 Cutover 流程

1. Migration apply → tables 建好
2. seed policy_version v1（即 ivy-backend repo 內已有的隱私權說明，先 commit 一份 markdown 進 `docs/privacy-policy-2026-05-28.md` 並 deploy 到 supabase storage）
3. 部署後家長 LIFF 進入 → modal 強制簽 v1 → 寫入 consent log
4. 員工 portal 同步上線（無 consent 但可 export）
5. 1 週後 review consent 簽署率，若 < 80% 發 LINE 廣播提醒

### 3.5 不變的契約

- 既有 LIFF login flow 不變（modal 是登入後第一個 view）
- 既有 data-export endpoint 不變
- 既有 portal endpoints 不變（員工 portal 加新 endpoint）

---

## 4. 測試策略

### 4.1 Schema/Model tests
- migration up + down 完整
- ParentConsentLog / PolicyVersion 基本 CRUD

### 4.2 Backend API tests
- consent log 寫入 + 查詢 + 撤回
- policy 升版後既有 user 強制重簽 detection
- delete-request 建立 + admin approve/reject
- correct-request 建立 + admin approve（apply diff to student record + audit log）
- opt-out scope 撤回後對應功能受限（如撤 photo_publish 後 photos endpoint 仍可看舊照片但不再推新）
- employee data-export 完整性（含薪資、考勤、請假）+ rate limit（1/小時/user）

### 4.3 Frontend tests
- LoginView consent modal 顯示 / 簽署 / 強制重簽
- MeView DSR section 4 個操作（下載/刪除/更正/同意紀錄）
- Admin DsrRequestsView queue 操作

### 4.4 E2E (workspace `e2e/`)
- 家長首次登入 → 簽 consent → 進 MeView 查同意紀錄
- 家長申請刪除 → admin approve → 確認 lifecycle 變更
- Employee export 下載 → assert JSON 含 salary_records

---

## 5. Rollout

1. **PR1 (BE schema + model)**: alembic migration + model + 基本 CRUD endpoint，無前端
2. **PR2 (BE consent + DSR endpoints)**: 完整 API + tests
3. **PR3 (FE parent consent modal + MeView DSR)**: 對接 consent + DSR
4. **PR4 (FE admin queue + policy versions)**: admin 端管理介面
5. **PR5 (FE employee portal data-export)**: 員工端
6. **Cutover**: seed v1 policy → 公告 → enable consent gate

每 PR 各自 CI 通過、merge 後再進下一個。

---

## 6. Risk & Trade-offs

### 6.1 已接受的 Risk

| Risk | 接受理由 | Follow-up |
|------|---------|-----------|
| 既有家長 cutover day 強制重簽，未簽者 portal 進不去 | 法律不允許追溯同意；可接受 1 週 friction | LINE 廣播 + 客服協助 |
| Delete request 走 admin review 非立刻硬刪 | 學生資料合班/稅務需保留 | request 進 admin queue 後家長即收 LINE 通知預期處理時間 |
| 員工 DSR 只做 export，v1 不做 delete | 勞基法 §80 5 年保存 | next sprint 評估 |
| Policy 升版要求所有 user 重簽 | 法律最佳實踐 | 5% 採樣監測簽署率 |
| 跨境傳輸 scope 同意僅針對 Supabase US region | 若改 region 需重簽 | 部署文件記錄 region |

### 6.2 對 P0a / P0b / P0d 的影響

- P0a 無互動
- P0b：consent log 寫入會走 audit，redaction 對 `consented_at` / `ip_address` 不遮（系統欄位）
- P0d 依賴此 sprint 完成的「reason 欄位 + audit-trail 化操作」pattern（醫療欄位讀取要 reason）

---

## 7. 驗收條件

1. 新家長 LIFF 登入 → consent modal 強制 → 4 scope 簽署 → `parent_consent_log` 對應寫入
2. Admin 上傳新 policy version → 既有 user 下次登入 modal 強制重簽
3. 家長 MeView 申請刪除 → admin queue 出現 → approve 後 student lifecycle 改 `delete_requested`
4. 家長撤回 photo_publish scope → `parent_consent_log` 寫入 consented=false
5. 員工 portal export → 下載 JSON 含完整薪資 / 考勤 / 請假 history
6. 既有 pytest 5103+ 全綠 + 新增 ParentConsentLog / DSR / consent / portal export test 全綠
