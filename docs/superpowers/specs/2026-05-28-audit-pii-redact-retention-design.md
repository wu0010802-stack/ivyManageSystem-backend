# Audit Log PII Redaction + Retention GC（P0b）

**日期**: 2026-05-28
**範圍**: ivy-backend
**Sprint**: P0b（4 個 P0 法規/個資 sprint 中的第二個）
**預估**: 3-4 工作天

---

## 1. 背景與動機

`audit_logs.changes` 目前以明文 JSON 落庫，UPDATE student/employee 會把 before/after 含身分證/電話/銀行帳號全文寫入。`security_gc_scheduler` 只 GC `rate_limit_buckets` 與 `jwt_blocklist`，audit_logs 永久保留。

**現況證據：**
- `utils/audit.py:509 / 564 / 628` 三處 `json.dumps(changes, ...)` 直寫無遮罩
- `services/security_gc_scheduler.py:31` 只跑 rate_limit (60min) + jwt_blocklist (6h)，audit_logs 沒有 GC

**風險**：DB dump 外洩時 audit 表比主表更危險（含歷史 before 值）；查核工具反而成最大個資礦坑。違反個資法 §11（特定目的消失應主動刪除）、GDPR Art. 5(1)(e) storage limitation。

---

## 2. 目標與非目標

### 目標
1. **PII 遮罩**：所有 `audit_logs.changes` 寫入前，PII 欄位 value 替換為 `[Filtered]`，保留 key（讓 audit 仍能追蹤「哪個欄位被改」）。
2. **金流 audit 不損失**：amount 類欄位（salary, bonus, fee_amount）value 保留（audit 需要金額變化作稽核憑證）。
3. **Retention GC**：依 entity_type 分級保留——金流 7 年（稅法保存期）/ 學生資料 3 年 / 登入 6 個月 / 其他 3 年。
4. **單一資料源**：複用 Sentry `_PII_KEY_SUBSTRINGS` + `_PII_KEY_EXEMPT_SUBSTRINGS`（CLAUDE.md #8），不引入第三份名單。

### 非目標
1. **既有 audit_logs 歷史資料 retroactive redaction**：不對 DB 既有列洗資料（資料量大、影響線上）。Risk-accepted：retention GC 跑起來後，最舊資料會自然消失；新寫入即遮。
2. **`summary` 欄位遮罩**：summary 是「審核薪資記錄」這類人類可讀標題，多為 entity_type 中文化，不含 PII，不動。
3. **Audit log 寫入 column 加密**：暫不加密整列（仍靠 DB 層 at-rest encryption）。redaction-at-write 已大幅降低 dump leak 風險。
4. **送出時 redaction（read path）**：admin UI 讀 audit 不再遮（落庫已遮）。

---

## 3. 設計

### 3.1 新增 `utils/audit_redact.py`

**純函式 helper**，無 I/O：

```python
"""utils/audit_redact.py — audit_logs.changes PII 遮罩。

複用 utils/sentry_init 的 _PII_KEY_SUBSTRINGS + _PII_KEY_EXEMPT_SUBSTRINGS。
針對 audit 額外維護 _AUDIT_VALUE_KEEP_SUBSTRINGS：amount 類欄位 value 保留，
因為 audit 需要金額變化軌跡（誰改了 50000 → 80000）才有稽核價值。
"""
from utils.sentry_init import _PII_KEY_SUBSTRINGS, _PII_KEY_EXEMPT_SUBSTRINGS

_FILTERED = "[Filtered]"

# Audit 例外：amount 類欄位 value 保留（金流稽核需要）
# 注意：bank_account / card_no / id_number 等仍應遮
_AUDIT_VALUE_KEEP_SUBSTRINGS: frozenset[str] = frozenset({
    "salary_amount", "bonus_amount", "fee_amount", "total_amount",
    "gross_salary", "net_salary", "deduction_amount", "payment_amount",
    "overtime_amount", "leave_payout_amount", "insured_amount",
    # 注意：不含 bank/card/id_number — 那些是識別子不是金額
})


def _should_redact(key: str) -> bool:
    """key 應否遮罩：Sentry denylist 命中 AND 不在 audit-keep AND 不在 sentry-exempt"""
    key_lower = key.lower()
    if any(s in key_lower for s in _PII_KEY_EXEMPT_SUBSTRINGS):
        return False
    if any(s in key_lower for s in _AUDIT_VALUE_KEEP_SUBSTRINGS):
        return False
    return any(s in key_lower for s in _PII_KEY_SUBSTRINGS)


def redact_pii(changes: dict | list | None) -> dict | list | None:
    """遞迴遮罩 changes。命中 key 的 value 替換為 [Filtered]，保留 key。

    支援結構：
      - dict: 對 value 遞迴
      - list: 對每個 item 遞迴
      - before/after diff: {"field": {"before": x, "after": y}} → 對 field 名稱判斷
      - 純值（str/int/None）: 直接回傳

    Edge cases:
      - 巢狀含 PII 欄位（如 changes.student.id_number）:遞迴遮罩
      - 不破壞型別（dict in → dict out）
    """
    if changes is None:
        return None
    if isinstance(changes, list):
        return [redact_pii(item) for item in changes]
    if not isinstance(changes, dict):
        return changes

    result = {}
    for key, value in changes.items():
        if _should_redact(key):
            result[key] = _FILTERED
        elif isinstance(value, (dict, list)):
            result[key] = redact_pii(value)
        else:
            result[key] = value
    return result
```

### 3.2 整合 `utils/audit.py` 三處寫入點

在每處 `json.dumps(changes, ...)` 之前插入 `changes = redact_pii(changes)`：

**Site 1**: `write_in_session_audit` (line 506-509)
**Site 2**: `write_explicit_audit` (line 561-570)
**Site 3**: `write_login_audit` (line 626-634)

```python
# Before:
if changes is not None:
    try:
        changes_json = json.dumps(changes, ensure_ascii=False, default=str)

# After:
if changes is not None:
    from utils.audit_redact import redact_pii
    changes = redact_pii(changes)
    try:
        changes_json = json.dumps(changes, ensure_ascii=False, default=str)
```

middleware 寫入路徑也經過這三個 helper 之一，不需單獨改。

### 3.3 Retention GC：擴充 `services/security_gc_scheduler.py`

**Retention policy** （by `audit_logs.entity_type`）：

| 範疇 | entity_type | 保留期 | 法源 |
|------|-----------|------|------|
| 金流稅務 | `salary`, `fee`, `fee_record`, `overtime`, `vendor_payment`, `year_end`, `salary_record`, `payslip`, `bonus`, `appraisal_year_end` | 7 年 | 稅捐稽徵法 §30 帳簿憑證 |
| 認證 | `auth` | 6 個月 | 個資法 §11 必要範圍 |
| 學生/員工資料 | `student`, `employee`, `guardian`, `parent`, `classroom`, `enrollment`, `recruitment`, `appraisal`, `attendance`, `leave`, `medical` | 3 年 | 個資法 §11 + 兒少保護記錄 |
| Fallback | 其他全部 | 3 年 | 保守 default |

**新增** `_run_audit_log_gc()`：
- 跑頻率：每日一次（heartbeat 60s 內檢查 `last_run > 24h`）
- 用 advisory_lock 防多 worker 並發
- SQL 為 batched DELETE（每批 10000 列）避免長交易：
  ```sql
  DELETE FROM audit_logs WHERE id IN (
      SELECT id FROM audit_logs
      WHERE created_at < :cutoff
        AND entity_type = :entity_type
      LIMIT 10000
  )
  ```
- 用 entity_type 分組批次跑（每 entity_type 自己的 cutoff），不一次 DELETE 全部
- 記錄總刪除筆數到 `scheduler_observability.record_rows`

**Settings 加 enable flag**（新增到 `config/scheduler.py`，default `True`）：
- `AUDIT_GC_ENABLED=true` → 跑 GC
- `AUDIT_GC_ENABLED=false` → 整段跳過（HR 簽合規 SOP 前可暫關）

### 3.4 不變的契約

- `audit_logs` schema：不動（不加 column、不改 nullable）
- audit 寫入 API 簽章：`changes: dict | None` 不變，redaction 對 caller 透明
- admin UI 讀 audit：不變（讀到的就是已遮版本）
- middleware 行為：不變

---

## 4. 測試策略

### 4.1 Unit tests `tests/test_audit_redact.py`（新檔）

1. **基本遮罩**: `redact_pii({"id_number": "A123"})` → `{"id_number": "[Filtered]"}`
2. **保留 amount**: `redact_pii({"salary_amount": 50000})` → `{"salary_amount": 50000}`（不遮）
3. **Sentry exempt 不遮**: `redact_pii({"ip_address": "1.2.3.4"})` → 保留（system 欄位）
4. **遞迴 dict**: `redact_pii({"student": {"id_number": "X"}})` → `{"student": {"id_number": "[Filtered]"}}`
5. **before/after diff 結構**: `redact_pii({"phone": {"before": "1234", "after": "5678"}})` → `{"phone": "[Filtered]"}` （遮整個 phone field 不論 nested 結構）
6. **list 遞迴**: `redact_pii([{"phone": "x"}, {"name": "y"}])` 處理每個 item
7. **None / 非 dict**: 原值回傳
8. **保留 key**: 遮罩後 dict 仍有所有 key（讓 audit 仍能追蹤）
9. **bank_account / card_no 仍遮**: 雖在金流範疇但是識別子需遮
10. **大型 nested**: 5 層巢狀正確遞迴
11. **混合 keep 與 redact**: `{"salary_amount": 50000, "bank_account": "X"}` → `{"salary_amount": 50000, "bank_account": "[Filtered]"}`

### 4.2 Integration tests `tests/test_audit.py`（擴充既有檔）

1. **write_in_session_audit 自動遮罩**: 傳 changes={"id_number": "A123"} → DB 查到 changes JSON 含 `"id_number": "[Filtered]"`
2. **write_explicit_audit 自動遮罩**: 同上
3. **write_login_audit 自動遮罩**: extras 含 username（保留）+ 假設未來加 phone（遮罩）
4. **既有 audit 寫入 regression**: 確認既有 5103 pytest 全綠
5. **JSON size limit 仍生效**: 64KB truncate 在 redact 後仍照常

### 4.3 GC tests `tests/test_security_gc_scheduler.py`（擴充）

1. **金流類 7 年 retention**: 插入 7 年 1 天前的 `entity_type='salary'` log → GC 後刪除
2. **金流類 7 年內保留**: 插入 6 年前的 `entity_type='salary'` log → GC 後仍在
3. **登入 6 個月 retention**: 插入 7 個月前的 `entity_type='auth'` log → 刪除
4. **學生資料 3 年 retention**: 插入 3 年 1 天前的 `entity_type='student'` log → 刪除
5. **未知 entity_type fallback 3 年**: 插入未知 type 4 年前 log → 刪除
6. **AUDIT_GC_ENABLED=false 不跑**: 設 flag → GC 0 deletion
7. **Batched delete**: 插入 15000 列符合刪除條件 → 分 2 批 (10000+5000) 刪
8. **Advisory lock 互斥**: 同時開兩個 session 模擬多 worker → 只有一個拿到 lock

### 4.4 手動驗證（PR merge 前）

1. local 模擬 UPDATE student：改身分證 → `SELECT changes FROM audit_logs ORDER BY id DESC LIMIT 1` → 確認 id_number value 為 `[Filtered]`
2. 改 salary amount → 確認 amount value 保留
3. 跑一次 GC（manual 觸發 `_run_audit_log_gc`）→ 確認刪除行為

---

## 5. Rollout

1. **PR**: 含 `utils/audit_redact.py` + 三 site integration + GC + tests
2. **CI**: 全 pytest 通過 + 既有 5103 test no regression
3. **No schema migration**：純 code change
4. **No frontend change**
5. **Settings 預設**：`AUDIT_GC_ENABLED=true`（drop-in 啟用），但 HR 可在 prod env 暫設 `false` 觀察一週
6. **Prod 部署順序**：先部署但 `AUDIT_GC_ENABLED=false`，confirm redaction 生效後第二週開 GC
7. **Merge & push**：完整功能 PR

---

## 6. Risk & Trade-offs

### 6.1 已接受的 Risk

| Risk | 接受理由 | Follow-up |
|------|---------|-----------|
| 既有 audit_logs 歷史 PII 仍明文 | retroactive update 影響線上、且 retention GC 跑起來後最舊資料會自然消失 | 如監理要求加速：寫獨立 `scripts/backfill_audit_redact.py` 一次性離線 redact，分批 + 限速 |
| redaction substring 匹配誤遮系統欄位 | 沿用 Sentry exempt 列表，CLAUDE.md #8 已記錄需新增 PII 同步檢查 | 同 P0a：新增 PII 欄位兩端 + audit 都要改 |
| GC 7 年 / 3 年是 default，個別法規可能要求更短 | 個資法 §11 「特定目的消失」是相對寬鬆解讀；金流 7 年是稅法上限 | 法務 review 後可在 settings 調整 |
| `summary` 不遮 | summary 為「審核薪資記錄」類人類可讀標題，當前 caller 不嵌入 PII；若未來有 caller 把 username 拼進 summary 才會洩漏 | follow-up 加 lint：禁 summary 含 f-string PII fields |

### 6.2 與 P0a 的差異

P0a 處理 binary（image bytes），P0b 處理 structured JSON（changes dict）。兩者獨立但都復用 Sentry denylist 精神（key substring matching）。

### 6.3 amount-keep 列表的維護

`_AUDIT_VALUE_KEEP_SUBSTRINGS` 是個小型 inclusion list，若未來新增 amount-類欄位需加入。這是有意的——預設遮罩，例外白名單，security-by-default。

---

## 7. 與其他 P0 sprint 的關係

| Sprint | 依賴 P0b？ | 說明 |
|--------|----------|------|
| P0a EXIF strip | 否 | 獨立完成 |
| P0c Consent + DSR | 否 | consent_log 寫入會走 audit，遮罩是 by-product |
| P0d 醫療加密 | 部分 | 醫療欄位變更會寫 audit → redact 會擋；P0d 額外 medical_access_log 也用 redact |

---

## 8. 驗收條件

PR merge 進 main 並 ship 後，下列全部成立：
1. UPDATE student.id_number → `audit_logs.changes` 該欄位 value=`[Filtered]`
2. UPDATE salary_record.amount → changes 中 amount value 保留為數字
3. login 失敗 → changes 中 username 保留、ip_address 保留（system 欄位）
4. AUDIT_GC_ENABLED=true → 跑一次 GC 後，7 年以上金流 audit / 6 月以上登入 audit / 3 年以上其他 audit 被刪
5. AUDIT_GC_ENABLED=false → GC 完全跳過
6. 既有 pytest 5103+ 全綠 + 新增 audit_redact / audit / scheduler test 全綠
