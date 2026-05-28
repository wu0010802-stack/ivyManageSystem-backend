# Observability Forensic Readiness, Data Quality Invariants, Deep Health Probe, and Frontend Token Canonical — Design

**日期**：2026-05-28
**負責**：wu0010802
**狀態**：Draft — 待 user 審 spec 後進 writing-plans

---

## 背景

2026-05-28 P2 audit 列出 4 條 finding（編號 8/9/10/11）：

- **#8** AuditLog forensic readiness 不足：欄位缺 IP / UA / session_id，敏感 GET（家長端 health/medication/portfolio/contact_book detail）未記 audit_summary。
- **#9** 資料一致性偵測只覆蓋才藝 POS 對帳（`finance_reconciliation_scheduler.py`）與 PDF orphan recovery（`pdf_recovery.py`），其他跨表 invariant（員工離職未關旗標、學生鬼 row、Guardian/ContactBook 孤兒）無人盯。
- **#10** Healthcheck 太淺：`/health/ready` 只 `SELECT 1`，外部呼叫無 circuit breaker、Supabase 無 timeout。**註**：本日已 ship `P1 外部整合韌性`（circuit breaker / LINE retry / Supabase retry+fallback / LineTokenHealth daily ping / `/api/internal/integrations/health`），audit #10 大部分已落地，僅剩 `/health/ready` 仍是 shallow 探活 — 本 spec 收尾此一塊。
- **#11** Token 多套並存無 canonical：實測 `--color-*` 881、`--space-*` 823、`--text-*` 711、`--el-*` 441、`--pt-*` 194、`--neutral-*` 159、`--brand-*` 43、`--ivy-*` 36 等 6 套色彩相關 prefix，`globals.css` 用三層 fallback `--pt-surface-mute: var(--ivy-leaf-bg, #f5fbe6)` 把問題藏起來。

本 spec 統整四項處理，分四章節、四個 PR 依序 ship（Ch1→Ch4→Ch3→Ch4→Ch5）。

## 非目標（YAGNI）

- **不建** `auth_sessions` 表：用 stateless JWT `jti` claim 達成 session 識別，不引入伺服端 session 生命週期管理。
- **不批次** sed 改 882 處 `--color-*` / `--brand-*` 等業務 CSS：本 spec 只立 stylelint 規矩 + 量化技債，避免一次性大改災難（Element-plus 生態既有 `--el-*` override 需逐項驗證）。
- **不補** 教師端 / 員工端 sensitive GET audit：本 spec 限定 parent-end forensic scope；員工端越權偵測屬於另一條 audit。
- **不打** 真實 LINE introspect / Supabase HEAD bucket：P1 已有 daily 08:00 LINE token ping 寫 `LineTokenHealth` 表 + breaker state，readiness probe 直接讀（避免每 5-10s 一次 probe 撐爆 LINE 額度 / Supabase rate limit）。
- **不擴** 5 條 data quality rule 之外的 invariant：起步控制範圍，第二批 rule 為 follow-up。

## 整體架構與落地單元

| Ch | 處理 finding | 主要組件 | Migration | PR | Repo |
|---|---|---|---|---|---|
| **Ch1** | #8 | `models/audit.py` +2 欄、JWT jti、audit middleware 取值、4 條 parent GET 補 audit_summary | `auditfor01` | BE PR-A | ivy-backend |
| **Ch2** | #9 | `models/data_quality.py` 新表、5 條 invariant rule、03:00 scheduler、4 線 dispatch、4 endpoint + 1 run-now、2 Permission、ROLE_TEMPLATES、admin DataQualityView | `dqreport01` | BE PR-B + FE PR-E | ivy-backend + ivy-frontend |
| **Ch3** | #10 剩尾 | `api/health.py` 加 `deep` query、3 component check helper | — | BE PR-C | ivy-backend |
| **Ch4** | #11 | `docs/TOKENS.md`、`.stylelintrc.cjs` 新 rule（warn level）、量化 baseline script、CI lint step | — | FE PR-D | ivy-frontend |

**依序與依賴**：PR-A → PR-B → PR-C → PR-D → PR-E。
- PR-B 依賴 PR-A（data_quality run 也會寫 audit_log，需先有 jti 機制以維持 forensic 一致）。
- PR-E 依賴 PR-B（OpenAPI codegen 需 PR-B 的 endpoint）。
- PR-C 與 PR-D 可獨立於前兩者並行 ship。

---

## Ch1 — AuditLog Forensic Readiness（#8）

### 1.1 Schema 變動

`models/audit.py`：

```python
user_agent_hash = Column(String(64), nullable=True, comment="SHA256(UA)[:32]，避免直存 device PII")
session_id = Column(String(64), nullable=True, index=True, comment="JWT jti claim")
```

理由：
- `user_agent_hash` — `User-Agent` header 含 device 指紋（手機型號、OS 版本、app 版本），落地會觸及既有 Sentry PII denylist 邊緣。Hash 化保留 forensic 用途（同人換 device 可 diff 出來），又避開直接外洩。
- `session_id` — 用 JWT jti（stateless），不引伺服端 session 表。

Migration `auditfor01`，沿用既有 alembic single head 鏈接。

### 1.2 JWT jti claim 注入

`utils/auth.py` token 生成處：
- access token / refresh token 各自獨立 jti（同一 user 可有多 jti，正是「同一人」「不同 session/device」的證據鏈）
- `refresh_access_token` 時新 access token 拿新 jti
- 來源：`secrets.token_urlsafe(16)`（22 字元，比 UUID 短，足夠唯一）

### 1.3 Audit Middleware 取值

`utils/audit_middleware.py` 既有檔，於寫 AuditLog 處：

```python
ua = request.headers.get("user-agent", "")
ua_hash = (
    hashlib.sha256(ua.encode("utf-8", errors="ignore")).hexdigest()[:32]
    if ua else None
)
session_id = jwt_payload.get("jti") if jwt_payload else None
```

`ip_address` 已落地，無變動。Sentry denylist 確保 raw `user-agent` 不外洩 — 仍由 `_PII_KEY_SUBSTRINGS` 攔截，本 spec 不改 denylist。

### 1.4 補 audit_summary 給 4 條 parent-end detail GET

| Endpoint | Summary |
|---|---|
| `GET /portal/parents/me/children/{child_id}/health` | `家長 {parent_id} 查看 {child_name} 健康紀錄` |
| `GET /portal/parents/me/children/{child_id}/medications/{med_id}` | `家長 {parent_id} 查看 {child_name} 用藥紀錄 #{med_id}` |
| `GET /portal/parents/me/children/{child_id}/portfolio` | `家長 {parent_id} 查看 {child_name} 學習歷程（含 PDF）` |
| `GET /portal/parents/me/children/{child_id}/contact-book/{entry_id}` | `家長 {parent_id} 查看 {child_name} 聯絡簿 #{entry_id}` |

實作：沿用 `api/portal/medications.py:190` 既有 `audit_summary` pattern（list 已記，補 detail 即可）。

### 1.5 量級評估

每園所 ~80 家長 × ~3 次/週 × 4 endpoint ≈ 3,840 row/週 ≈ 200K row/年。既有 365 天 GC 保持 < 200K row，indexed by `user_id` / `entity_type+entity_id` / `created_at`，查詢成本可控。

### 1.6 測試（3 個 pytest）

1. `test_audit_middleware_writes_ua_hash_and_jti` — 模擬 request → audit_log row 含正確 hash + jti。
2. `test_jwt_jti_unique_per_token` — login → refresh → 兩 jti 不同。
3. `test_parent_get_emits_audit` — parent token GET health/medication/portfolio/contact-book → audit_logs +4。

---

## Ch2 — Data Quality Scheduler（#9，本 spec 最大章節）

### 2.1 新表 `data_quality_reports`

`models/data_quality.py`：

```python
class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"
    __table_args__ = (
        Index("ix_dqr_rule_detected", "rule_code", "detected_at"),
        Index("ix_dqr_status_severity", "status", "severity"),
        Index(
            "ix_dqr_dedup_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    rule_code = Column(String(64), nullable=False)
    severity = Column(String(4), nullable=False)         # "P0" | "P1" | "P2"
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(50), nullable=False)
    summary = Column(Text, nullable=False)               # 人類可讀（不含 PII）
    detected_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    last_seen_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    dedup_key = Column(String(64), nullable=False)       # sha256(rule_code:entity_type:entity_id)[:32]
    status = Column(String(10), default="open", nullable=False)
    ack_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ack_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)
```

`ix_dqr_dedup_open` partial unique 確保「同 entity 同 rule open 狀態只一筆」— 防 race。

Migration `dqreport01`，鏈接 `auditfor01`。

### 2.2 模組結構

```
services/data_quality/
├── __init__.py
├── engine.py             # run_all_rules(session) → list[Violation]，協調 rule 跑與 dispatch
├── dispatch.py           # _emit_log() / _persist() / _emit_sentry() / _emit_line() 4 線
├── scheduler_step.py     # async loop step（注入既有 scheduler.py）
└── rules/
    ├── __init__.py
    ├── _base.py          # Rule abstract: code/severity/description + check(session) -> list[Violation]
    ├── employee_offboard.py
    ├── student_stale_active.py
    ├── contact_book_orphan.py
    ├── guardian_orphan_user.py
    └── salary_no_employee.py
```

`Violation = NamedTuple(rule_code, severity, entity_type, entity_id, summary)`。每條 rule 一檔，便於後續增刪。

### 2.3 起步 5 條 rule

| code | severity | 偵測邏輯 |
|---|---|---|
| `employee_active_but_offboarded` | P1 | `Employee.is_active = TRUE AND offboard_date IS NOT NULL AND offboard_date <= CURRENT_DATE` |
| `student_active_but_lifecycle_terminal` | P1 | `Student.is_active = TRUE AND lifecycle_status IN ('GRADUATED','WITHDRAWN','TRANSFERRED')` |
| `contact_book_orphan_student` | P0 | `ContactBookEntry LEFT JOIN students ON student_id WHERE students.id IS NULL` |
| `guardian_orphan_user` | P0 | `Guardian.user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users)` |
| `salary_record_orphan_employee` | P0 | `SalaryRecord.employee_id NOT IN (SELECT id FROM employees)` |

**為何用 `lifecycle_status` 而非 audit 原 `enrollment_date > 5y`**：codebase（CLAUDE.md #9）規定 `Student.lifecycle_status` 變更必經 `utils/student_lifecycle.set_lifecycle_status`，是 source of truth；用 enrollment_date 推 active 會與 FSM 不一致。

### 2.4 Scheduler 注入

既有 `scheduler.py` async polling loop 加 `data_quality_step()`：
- 每日 03:00 Taipei tz 跑一次（避開 02:00 finance reconciliation、02:17 dr-backup、08:00 LineTokenHealth ping）
- 啟用 flag `data_quality_enabled`（沿用 `config/scheduler.py` pattern），**預設 False** — HR 確認 5 條 rule 跑出來不灌 LINE 噪音再 enable
- 用既有 `try_scheduler_lock` 防多 worker 並發

### 2.5 4 線 dispatch

每個 Violation 流經：

```python
def emit(violation, session):
    _log(violation)                            # 1. logging.getLogger("data_quality").warning(...)
    row, is_new_open = _persist(violation, session)  # 2. 寫表（含 dedup 邏輯）
    if is_new_open:                            # 3+4 僅新 open 才發
        _emit_sentry(violation)
        _line_queue.append(violation)          # 累積成 digest

# 一輪 rules 跑完後
_send_line_digest(_line_queue)                 # 5 條以下合一則 flex，超過列首 3 條 + 「另 N 條」
```

**dedup 規則**：
- 進來時 `SELECT ... WHERE dedup_key = ? AND status = 'open' FOR UPDATE`
- 找到 → `UPDATE last_seen_at = NOW()`，不 emit Sentry/LINE，不寫新 row
- 沒找到 → `INSERT` new row，`is_new_open = True`
- `ignored` 狀態的也視為「不重發」（HR 標記過的不再吵）
- `fixed` 狀態的不阻塞 — 同 entity 再次違規會開新 row（業務上有意義：「修了又壞」是強信號）

**Sentry**：`capture_message(severity_to_sentry_level[severity], tags={"rule_code": v.rule_code, "entity_type": v.entity_type})`。tags 進 Sentry search 用，**不放 PII**。

**LINE**：用 P1 已 ship 的 `LineService.push_flex_to_user` + 紅色 header `「資料品質告警 {detected_at}」`，body 列首 3 條 violation summary + 「另 {N} 條請至後台查看」+ button → admin DataQualityView URL。LINE 失敗走 P1 retry scheduler（不另外做）。

### 2.6 Permission + Endpoint

新 `Permission`：
- `DATA_QUALITY_READ`（看 reports）
- `DATA_QUALITY_WRITE`（ack/resolve/ignore/run-now）

ROLE_TEMPLATES：`admin` / `principal` 兩個 role 加二者（前後端 `PERMISSION_LABELS` 同步）。

Endpoint（沿用既有 router pattern + PR #43 共用分頁 helper）：

| Method | Path | Permission | Body |
|---|---|---|---|
| GET | `/api/data-quality/reports?status=&rule_code=&severity=&page=&page_size=` | READ | — |
| POST | `/api/data-quality/reports/{id}/ack` | WRITE | `{note?: string}` |
| POST | `/api/data-quality/reports/{id}/resolve` | WRITE | `{note: string}` |
| POST | `/api/data-quality/reports/{id}/ignore` | WRITE | `{note: string}` |
| POST | `/api/data-quality/run-now` | WRITE | — |

`run-now` 手動觸發（HR 改完資料想立刻 verify）— 同 scheduler 邏輯但同步回 200。

### 2.7 前端 admin view

新檔 `src/views/DataQualityView.vue`（主選單第 N 個 tab，permission gate `DATA_QUALITY_READ`）：

- top：counter chip（open P0=3 / P1=12 / P2=0）
- filter bar：status / rule_code / severity
- table：detected_at / rule_code / severity tag / entity link / summary / status / actions
- modal：點 row 看 detail + ack/resolve/ignore 操作

新檔 `src/api/dataQuality.ts`（沿用 typed.d.ts pattern，5 函式對應 5 endpoint）。

### 2.8 測試

| 測試 | 數量 |
|---|---|
| 每條 rule 1 個（測 detect + non-detect） | 5 |
| `engine.run_all_rules` 整合 | 1 |
| `dispatch.emit` dedup（同 dedup_key 不重發） | 1 |
| scheduler step（flag off 跳過 / flag on 跑） | 2 |
| 4 endpoint + run-now | 5 |
| Permission gate（無 DATA_QUALITY_READ → 403） | 1 |
| **小計** | **15** |

前端 vitest：DataQualityView render / filter / action 3 個。

### 2.9 與 CLAUDE.md 對齊

- **#9 PII GC**：DataQualityReport 不存 PII（summary 寫「員工 #42 離職未關旗標」用 id 不用 name），免進 365 天 GC 流程。
- **#1 權限**：兩新 Permission 是 str enum value，不是 IntFlag bit；ROLE_TEMPLATES 前後端同步。
- **Sentry denylist**：`rule_code` / `entity_type` / `dedup_key` 不是 PII，不需加 denylist；但 LINE digest body 含 summary 字串，於 `services/data_quality/rules/*` 內**禁止組裝 PII**（用 id 而非姓名）。

---

## Ch3 — Deep `/health/ready`（#10 剩尾）

### 3.1 API 變動

`api/health.py:readiness`：

```python
@router.get("/ready")
async def readiness(deep: bool = Query(False)):
    start = time.monotonic()
    components: dict[str, dict] = {}
    overall_ok = True

    # DB（shallow + deep 都跑）
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        components["db"] = {"ok": True}
    except Exception:
        components["db"] = {"ok": False}
        overall_ok = False

    if deep:
        components["line"] = _check_line()
        components["supabase"] = _check_supabase()
        components["db_pool"] = _check_db_pool()
        overall_ok = overall_ok and all(c.get("ok") for c in components.values())

    elapsed = round((time.monotonic() - start) * 1000, 1)
    body = {"status": "ok" if overall_ok else "degraded",
            "latency_ms": elapsed,
            "components": components}
    return JSONResponse(status_code=200 if overall_ok else 503, content=body)
```

### 3.2 三個 deep component 來源

| component | 來源 | 失敗條件 |
|---|---|---|
| `line` | `LINE_BREAKER.state` + 最近 24h `LineTokenHealth` row | `breaker.state == "open"` OR `last_check.healthy is False` |
| `supabase` | `SUPABASE_BREAKER.state` + `count(PendingUpload WHERE status='pending' AND attempts<5)` | breaker open OR pending_uploads > 50 |
| `db_pool` | `engine.pool.checkedout() / engine.pool.size()` | 使用率 > 0.85 |

### 3.3 關鍵設計：deep 不打外網

readiness probe 可能 5-10s 一次。若 deep 每次都 LINE introspect + Supabase HEAD：
- 撐爆 LINE 免費額度（500 ping/月 daily quota）
- 觸發 Supabase rate limit
- probe 自身成為 outage 來源

P1 ship 的 breaker state 已是「最近真實流量的健康鏡像」，比探活更準。

### 3.4 Response 範例

deep 部分綠：

```json
{
  "status": "degraded",
  "latency_ms": 12.3,
  "components": {
    "db": {"ok": true},
    "line": {"ok": false, "breaker": "open", "consecutive_failures": 6, "last_check_at": "2026-05-28T08:00:00+08:00"},
    "supabase": {"ok": true, "breaker": "closed", "pending_uploads": 3},
    "db_pool": {"ok": true, "used": 8, "size": 20, "utilization": 0.4}
  }
}
```

### 3.5 zeabur / LB 不動

readiness probe 仍打 `/health/ready` 不帶 query，shallow 行為一致；SRE 手測 `?deep=1` 看細節。

### 3.6 測試（3 個 pytest）

1. `test_ready_shallow_returns_db_only` — 不帶 query → response 只含 `db`，回 200。
2. `test_ready_deep_all_green_returns_200` — mock breaker closed + LineTokenHealth healthy + pool 低 → 200，4 component 全綠。
3. `test_ready_deep_line_breaker_open_returns_503_with_details` — mock LINE_BREAKER open → 503，body 含 line.breaker="open"。

---

## Ch4 — Frontend Token Canonical（#11，立規矩量化技債）

### 4.1 核心決策回顧

以 `--color-*`（881 處）為 raw 色票 source of truth；其他 5 套色彩相關 prefix 全轉 `var(--color-*)` alias。本 spec **不動** 既有 882+ 處業務 CSS，只立規矩量化技債。

### 4.2 新檔 `ivy-frontend/docs/TOKENS.md`

```markdown
# Design Tokens — Canonical Reference

## Source of Truth
- **--color-*** = raw palette（HEX / RGB），唯一允許定義原始顏色
- 其他色彩 prefix 全須以 var(--color-*) 形式 alias

## Token Tiers（命名分層）
| Tier | Prefix | 範例 | 是否允許新增 |
|---|---|---|---|
| Raw palette | `--color-*` | `--color-primary-500: #4a90e2` | ✅ 唯一來源 |
| Element-plus override | `--el-*` | `--el-color-primary: var(--color-primary-500)` | ⚠️ 只允許覆寫，禁新增業務 token |
| Brand alias | `--brand-*` | `--brand-primary: var(--color-primary-500)` | ❌ deprecated |
| Component shorthand | `--pt-*`, `--m3-*` | `--pt-surface-mute: var(--color-neutral-50)` | ❌ deprecated |
| Legacy raw | `--ivy-*`, `--neutral-*` | — | ❌ deprecated |

## Design Dimensions（非色彩 prefix，繼續用）
`--space-*` / `--text-*` / `--fs-*` / `--radius-*` / `--border-*` / `--dur-*` / `--ease-*` / `--shadow-*` / `--bg-*` / `--surface-*` / `--font-*` / `--transition-*` / `--touch-*`

## 遷移狀態
- 階段 1（本 PR）：lint warn，量化技債數
- 階段 2（follow-up）：hot files 批次 sed，warn → error
- 階段 3（follow-up）：deprecate prefix 全清，TOKENS.md 移除「deprecated」段
```

### 4.3 stylelint rule（warn level）

新檔 / 合併 `ivy-frontend/.stylelintrc.cjs`：

```js
module.exports = {
  plugins: ["./scripts/stylelint/canonical-token-prefix.js"],
  rules: {
    "ivy/canonical-token-prefix": [true, {
      severity: "warning",
      raw: ["color"],
      designDimensions: ["space","text","fs","radius","border","dur","ease",
                        "shadow","bg","surface","font","transition","touch"],
      elementPlusOverrideOnly: ["el"],
      deprecated: ["ivy","brand","pt","m3","neutral"]
    }]
  }
};
```

自訂 plugin `scripts/stylelint/canonical-token-prefix.js`：parse declaration value 中 `var(--xxx-...)`，xxx 屬 deprecated → warn 帶「請改用 var(--color-*)」訊息。

### 4.4 量化 baseline script

新檔 `scripts/lint-tokens.mjs`：

```js
// 跑 stylelint，輸出每個 prefix 當前使用次數 + 預估遷移工時
// 寫 .scratch/tokens-baseline.json（不入 repo，供 follow-up PR diff 用）
```

CI 階段 1 **不 fail**（continue-on-error: true 或 lint --max-warnings=10000），只 report。

### 4.5 既有 `globals.css` 三層 fallback 不動

audit 提到的 `--pt-surface-mute: var(--ivy-leaf-bg, #f5fbe6)` 保留 — 改它要動 component-level 用法，是階段 2 工作。階段 1 只在 `TOKENS.md` 標註此 chain 為 deprecated 範例。

### 4.6 測試

- vitest **不變**（純 lint config，無 runtime）
- 新增 `npm run lint:tokens` 在 package.json
- CI workflow 加一 step（continue-on-error: true 階段 1）

---

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| Ch1 audit_logs 增量 ~200K/年 對 prod DB 壓力 | indexed by user/entity/created_at，365 天 GC 已落地；上線 1 週後跑 `EXPLAIN ANALYZE` 確認查詢計畫不退化 |
| Ch2 5 條 rule 跑出來灌 LINE 噪音 | `data_quality_enabled` 預設 False；HR 先以 `run-now` 手動驗 baseline，標 `ignored` 清零後再 enable scheduler |
| Ch2 partial unique index dedup_key + status='open' 在 SQLite 不支援 | 既有 codebase 已有 PG-only partial index 慣例（`overtime_comp_leave_grants` 同 pattern），SQLite test 改用 app 層 `SELECT ... WHERE status='open'` 防護 |
| Ch3 deep readiness 被 LB 改打成預設 | 不動 zeabur/k8s probe 設定，文件 explicit 標註 `?deep=1` 為 SRE 手測用 |
| Ch4 stylelint warn level CI 不 fail，技債持續累積 | 量化 baseline + follow-up PR 強制比較「不比 baseline 多」；階段 2 升 error 設明確時間表（spec 完成後 4 週） |
| JWT jti 加 claim 對既有 token 不向下相容 | 既有 token 過期後（access 15 分鐘 / refresh 30 天）自然替換；audit_log 在過渡期 session_id 為 NULL 是預期，不阻塞功能 |

## 待補（spec 完成後 → writing-plans 處理）

- 各 chapter 拆 task 級行動
- 既有 audit middleware 寫入點 grep 確認所有路徑都會走到（避免漏 jti）
- 既有 `LINE_BREAKER` / `SUPABASE_BREAKER` 取用 API 對齊（P1 ship 的 `utils/circuit_breaker.py`）
- 既有 `scheduler.py` 注入點與其他 step 排程衝突確認
- 前端 OpenAPI codegen 流程驗證

## 變更摘要（LoC 估算）

| Unit | Repo | 新檔 | 改檔 | LoC 估 |
|---|---|---|---|---|
| Ch1 (PR-A) | ivy-backend | alembic/versions/auditfor01_*.py | models/audit.py, utils/auth.py, utils/audit_middleware.py, 4 portal router, 3 test | ~150 |
| Ch2 BE (PR-B) | ivy-backend | models/data_quality.py, services/data_quality/{engine,dispatch,scheduler_step,rules/*}.py, api/data_quality.py, alembic/versions/dqreport01_*.py, 15 test | utils/permissions.py +2 Permission, ROLE_TEMPLATES, scheduler.py 注入 | ~600 |
| Ch2 FE (PR-E) | ivy-frontend | src/api/dataQuality.ts, src/views/DataQualityView.vue, 3 vitest | router/index, main menu, schema.d.ts regen | ~300 |
| Ch3 (PR-C) | ivy-backend | 3 test | api/health.py | ~120 |
| Ch4 (PR-D) | ivy-frontend | docs/TOKENS.md, .stylelintrc.cjs, scripts/stylelint/canonical-token-prefix.js, scripts/lint-tokens.mjs | package.json, CI workflow | ~250 |
| | | | **合計** | **~1420** |
