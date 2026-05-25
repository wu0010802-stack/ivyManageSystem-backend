# 家長端 PII Retention + Data-Export 合規設計

**日期**：2026-05-22
**狀態**：spec（待 user 審閱 → writing-plans → 執行）
**範圍**：跨前後端（後端為主，前端僅 Me 頁加一個按鈕）
**法律驅動**：個資法第 11 條（特定目的消失應主動刪除）+ 第 10 條（當事人查閱權）

---

## 1. 問題陳述

家長端 LIFF 對畢業/轉出/退學學生「永久可讀」，無 PII retention 機制，亦無「當事人查閱權」endpoint。三項證據：

1. `api/parent_portal/_shared.py:75-85` `_assert_student_owned` 只在 `for_write=True` 擋終態 lifecycle，讀路徑全放行
2. `services/security_gc_scheduler.py` 只 GC `rate_limit_buckets`（60min retention）+ `jwt_blocklist`（已過期），無任何 PII GC
3. grep `data-export` / `me/data` / `當事人.*查閱` 全無命中，無 GDPR/個資法 §10 查閱權 endpoint

**多挖到一個 gap：** `Student` model 無 `terminal_entered_at` 時間戳——`updated_at` 任何欄位變更都會動不可靠——做任何 retention 政策前必須先有可靠的 transition 時間源。

---

## 2. 設計決策（user 已批准）

| 決策點 | 選擇 | 備註 |
|--------|------|------|
| Retention 年限 | 進入終態後 **365 天** | 金流、醫療、出席紀錄一個學年足以註銷；給家長合理備份視窗 |
| Hard-delete PII 範圍 | **Guardian 表 PII**（phone/email/custody_note/relation set NULL、name → `[已離校家長]`、user_id 解綁） | Student 本人 PII（姓名/生日）不動——那是學術學籍而非家長 portal 資料 |
| 二胎/復學 re-onboard | **不特殊處理**，admin 重新建 Guardian row | 復學（lifecycle → active）會 reset `terminal_entered_at=NULL` 取消 retention |
| Data-export 格式 | **同步 JSON**，三個模組一個檔，rate-limit 1/小時 | 滿足 §10 查閱權最小可行實作 |

**未問即決的合理 default：**
- 第一次 GC 跑前 **dry-run 模式**：列出將要 redact 的清單到 INFO log 但不執行；user 看 log 後改 ENV 啟用
- `audit_log` 寫入 `pii_redact` 動作但不存被刪的 phone/name（留戳記不留 PII）
- GC scheduler 開新檔 `services/pii_retention_scheduler.py`，不擴 `security_gc_scheduler`（分鐘級 vs 日級、邏輯複雜度差太遠）

---

## 3. 架構

```
┌───────────────────────────────────────────────────────────────┐
│                        Lifecycle 變更                          │
│  graduation_scheduler / student_enrollment / 其他 caller        │
│           ↓                                                    │
│  utils/student_lifecycle.set_lifecycle_status() ← 新 helper    │
│           ↓                                                    │
│  Student.lifecycle_status + Student.terminal_entered_at        │
│           ↓                                                    │
│  audit_log（lifecycle_change）                                 │
└───────────────────────────────────────────────────────────────┘
                            ↓ (365 天後)
┌───────────────────────────────────────────────────────────────┐
│              services/pii_retention_scheduler.py (新)          │
│  - 每 24 小時掃 students JOIN guardians                         │
│  - SKIP LOCKED 多 worker safe                                  │
│  - 抹 Guardian PII + 解綁 user_id                               │
│  - 寫 guardians.pii_redacted_at + audit_log                    │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│         api/parent_portal/data_export.py (新)                  │
│  GET /api/parent/me/data-export                                │
│  - LIFF JWT auth                                                │
│  - rate-limit 1/hr/user                                         │
│  - 同步聚合 JSON（contact_book/attendance/leaves/fees/         │
│    medications/photos/messages/growth_reports）                 │
│  - 50MB 上限                                                   │
└───────────────────────────────────────────────────────────────┘
```

---

## 4. Schema 變動（Alembic migration `pretent001`）

```python
# alembic/versions/pretent001_pii_retention_columns.py

def upgrade():
    op.add_column('students',
        sa.Column('terminal_entered_at', sa.DateTime(timezone=True), nullable=True,
                  comment='進入終態（graduated/transferred/withdrawn）的 UTC 時間戳；復學回 active 時 NULL；PII retention GC 計算用'))

    op.add_column('guardians',
        sa.Column('pii_redacted_at', sa.DateTime(timezone=True), nullable=True,
                  comment='Guardian PII 被 retention GC 抹除的時間戳；NOT NULL 即已抹過避免重複 GC'))

    # Partial index for GC scan
    op.create_index('ix_student_terminal_retention',
                    'students', ['terminal_entered_at', 'lifecycle_status'],
                    postgresql_where=sa.text('terminal_entered_at IS NOT NULL'))

    op.create_index('ix_guardians_pii_redacted_null',
                    'guardians', ['student_id'],
                    postgresql_where=sa.text('pii_redacted_at IS NULL'))

    # Backfill：現有終態學生從 audit_logs 找 lifecycle_status 最後變更時間
    # 找不到 fallback updated_at（有 false positive 風險但只影響當前已離校學生）
    # 注意：audit_logs.entity_id 是 String(50) 故 cast 為 int
    op.execute("""
        WITH lifecycle_changes AS (
            SELECT
                CAST(entity_id AS INTEGER) AS student_id,
                MAX(created_at) AS last_change_at
            FROM audit_logs
            WHERE entity_type = 'student'
              AND action IN ('UPDATE', 'CREATE')
              AND (changes LIKE '%lifecycle_status%' OR summary LIKE '%lifecycle%')
              AND entity_id ~ '^\\d+$'
            GROUP BY entity_id
        )
        UPDATE students s
        SET terminal_entered_at = COALESCE(lc.last_change_at, s.updated_at)
        FROM lifecycle_changes lc
        WHERE s.id = lc.student_id
          AND s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
          AND s.terminal_entered_at IS NULL;

        -- audit_logs 完全沒記錄的 fallback
        UPDATE students
        SET terminal_entered_at = updated_at
        WHERE lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
          AND terminal_entered_at IS NULL;
    """)

def downgrade():
    op.drop_index('ix_guardians_pii_redacted_null', 'guardians')
    op.drop_index('ix_student_terminal_retention', 'students')
    op.drop_column('guardians', 'pii_redacted_at')
    op.drop_column('students', 'terminal_entered_at')
```

**SQLite-aware**：partial index 在 SQLite 用 `postgresql_where` 會被忽略，僅 PG 套用——測試環境用 SQLite 不影響。

---

## 5. Lifecycle 時間戳寫入（新 helper）

```python
# utils/student_lifecycle.py

from datetime import datetime, timezone
from models.classroom import (
    Student,
    LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN,
)

_TERMINAL_LIFECYCLE = frozenset({LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN})

def set_lifecycle_status(
    session,
    student: Student,
    new_status: str,
    *,
    actor_user_id: int | None = None,
    audit: bool = True,
    reason: str | None = None,
) -> None:
    """原子化變更 lifecycle_status + 維護 terminal_entered_at + 寫 audit_log。

    所有 Student.lifecycle_status 變更必須走此 helper，不可直接 .lifecycle_status =。

    - 從非終態進入終態：terminal_entered_at = NOW()（utc）
    - 從終態回到非終態（罕見復學）：terminal_entered_at = NULL（取消 retention timer）
    - 終態→終態（如 transferred → graduated）：不更新 terminal_entered_at
    """
    old_status = student.lifecycle_status
    if old_status == new_status:
        return

    was_terminal = old_status in _TERMINAL_LIFECYCLE
    is_terminal = new_status in _TERMINAL_LIFECYCLE

    student.lifecycle_status = new_status

    if not was_terminal and is_terminal:
        student.terminal_entered_at = datetime.now(timezone.utc)
    elif was_terminal and not is_terminal:
        student.terminal_entered_at = None
    # else 不動

    if audit:
        import json
        from datetime import datetime
        from models.audit import AuditLog
        session.add(AuditLog(
            user_id=actor_user_id,
            username='scheduler' if actor_user_id is None else None,
            action='UPDATE',
            entity_type='student',
            entity_id=str(student.id),
            summary=f'lifecycle: {old_status} → {new_status}',
            changes=json.dumps({
                'old_status': old_status,
                'new_status': new_status,
                'reason': reason,
            }, ensure_ascii=False),
            ip_address=None,
            created_at=datetime.now(),
        ))
```

**Caller 改造（grep 找全部）：**
- `services/graduation_scheduler.py` 自動畢業
- `api/student_enrollment.py` admin 改 lifecycle
- `api/recruitment/...` 招生 funnel 進入 enrolled→active 等 transition（若有觸到終態）
- 其他直接寫 `student.lifecycle_status = X` 的 caller（grep 列出後逐處改）

---

## 6. PII Retention GC scheduler（新檔）

```python
# services/pii_retention_scheduler.py

"""
PII Retention GC：定期清除已超過 retention 期的家長 PII。

驅動：個資法第 11 條「特定目的消失應主動刪除」。

- 對象：Guardian 表中 student 已進終態且 terminal_entered_at < NOW - 365 天
- 動作：抹 phone/email/relation/custody_note，name 改 '[已離校家長]'，user_id 解綁
- 不刪 Guardian row（保留與 student 關聯供內部稽核）
- 不動 Student PII（學籍學術紀錄，retention 政策不同）
- 不刪 User row（user 可能還綁其他在學小孩）

環境變數：
- PII_RETENTION_GC_DISABLED=1 → 關閉本排程
- PII_RETENTION_GC_DRY_RUN=1 → 只 log 不執行（首跑驗證用）
- PII_RETENTION_TERMINAL_DAYS=365 → 可調 retention 天數（預設 365）

設計選擇：開新檔不擴 security_gc_scheduler，因 PII GC 是日級且邏輯複雜。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from config import get_settings
from models.database import get_session
from models.audit import AuditLog

logger = logging.getLogger(__name__)

_GC_INTERVAL_SEC = 24 * 60 * 60  # 每日
_BATCH_LIMIT = 500


def scheduler_enabled() -> bool:
    return not bool(get_settings().scheduler.pii_retention_gc_disabled)


def dry_run_enabled() -> bool:
    return bool(get_settings().scheduler.pii_retention_gc_dry_run)


def retention_days() -> int:
    return int(get_settings().scheduler.pii_retention_terminal_days or 365)


async def run_pii_retention_scheduler(stop_event: asyncio.Event) -> None:
    """主迴圈：每 24 小時跑一次 PII retention GC。"""
    logger.info("pii_retention_scheduler started (dry_run=%s, days=%s)",
                dry_run_enabled(), retention_days())
    # 啟動後 60 秒首跑（避免冷啟動同時打 DB）
    initial_delay = 60
    try:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass

        while not stop_event.is_set():
            _run_pii_retention_gc()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_GC_INTERVAL_SEC)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("pii_retention_scheduler stopped")


def _run_pii_retention_gc() -> None:
    """單次 GC：找到期 Guardian → 抹 PII → 寫 audit_log。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days())
    dry = dry_run_enabled()

    session = get_session()
    try:
        # SKIP LOCKED 多 worker safe
        rows = session.execute(text("""
            SELECT g.id, g.student_id, s.lifecycle_status, s.terminal_entered_at
            FROM guardians g
            JOIN students s ON s.id = g.student_id
            WHERE s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND s.terminal_entered_at IS NOT NULL
              AND s.terminal_entered_at < :cutoff
              AND g.pii_redacted_at IS NULL
              AND g.deleted_at IS NULL
            ORDER BY g.id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        """), {"cutoff": cutoff, "limit": _BATCH_LIMIT}).fetchall()

        if not rows:
            logger.info("pii_retention GC: 無到期 Guardian")
            return

        guardian_ids = [r[0] for r in rows]
        logger.info("pii_retention GC: %s 筆%s",
                    len(guardian_ids), " (dry-run)" if dry else "")
        for r in rows:
            logger.info("  - guardian_id=%s student_id=%s lifecycle=%s terminal_at=%s",
                        r[0], r[1], r[2], r[3])

        if dry:
            session.rollback()
            return

        # 抹 PII（單一 UPDATE atomic）
        session.execute(text("""
            UPDATE guardians
            SET name = '[已離校家長]',
                phone = NULL,
                email = NULL,
                relation = NULL,
                custody_note = NULL,
                user_id = NULL,
                pii_redacted_at = NOW(),
                updated_at = NOW()
            WHERE id = ANY(:ids)
        """), {"ids": guardian_ids})

        # 寫 audit_logs（每筆一條，changes 不含 PII）
        import json
        from datetime import datetime
        for r in rows:
            session.add(AuditLog(
                user_id=None,
                username='pii_retention_gc',
                action='UPDATE',
                entity_type='guardian',
                entity_id=str(r[0]),
                summary=f'PII retention redact (>{retention_days()}d after terminal)',
                changes=json.dumps({
                    'reason': f'retention_{retention_days()}d',
                    'student_id': r[1],
                    'lifecycle_status': r[2],
                }, ensure_ascii=False),
                ip_address=None,
                created_at=datetime.now(),
            ))

        session.commit()
        logger.info("pii_retention GC: 已抹 %s 筆 Guardian PII", len(guardian_ids))

    except Exception as e:
        logger.error("pii_retention GC 失敗: %s", e, exc_info=True)
        session.rollback()
    finally:
        session.close()
```

**`config/scheduler.py` 加三個欄位：**
```python
pii_retention_gc_disabled: bool = Field(default=True, alias='PII_RETENTION_GC_DISABLED')  # 預設關閉
pii_retention_gc_dry_run: bool = Field(default=True, alias='PII_RETENTION_GC_DRY_RUN')    # 預設 dry-run
pii_retention_terminal_days: int = Field(default=365, alias='PII_RETENTION_TERMINAL_DAYS')
```

**`main.py` 加 startup hook 啟動 scheduler**（同 graduation_scheduler 模式）。

---

## 7. Data-Export Endpoint

**檔案：** `api/parent_portal/data_export.py`（新）

**路徑：** `GET /api/parent/me/data-export`

**認證：** 同其他 parent_portal endpoint，LIFF JWT。

**Rate-limit：** 用 `utils/rate_limit.create_limiter`（依 `RATE_LIMIT_BACKEND` env 選 PG 多 worker safe 或 in-memory），module-level 定義 `_export_limiter = create_limiter(max_calls=1, window_seconds=3600, name="parent_data_export", error_detail="每小時限下載 1 次")`，endpoint 內 `_export_limiter.check(f"user:{user.id}")` 觸限自動 raise 429。

**回應：**
- `Content-Type: application/json`
- `Content-Disposition: attachment; filename="ivy_data_export_{user_id}_{YYYYMMDD}.json"`
- Body shape：
  ```json
  {
    "exported_at": "2026-05-22T08:30:00Z",
    "exported_by_user_id": 123,
    "schema_version": 1,
    "parent": {
      "display_name": "王小明",
      "line_user_id": "U..."
    },
    "students": [
      {
        "id": 456,
        "name": "王大寶",
        "birth_date": "2018-03-15",
        "lifecycle_status": "graduated",
        "guardian_role": {"name": "...", "relation": "...", "is_primary": true},
        "contact_book": [...],
        "attendance": [...],
        "leaves": [...],
        "fees": [...],
        "medications": [...],
        "photos": [...],
        "messages": [...],
        "growth_reports": [...]
      }
    ]
  }
  ```

**容量限制：** 序列化後 size > 50MB 回 `413 Payload Too Large`，detail 提示「資料量過大，請聯絡園所協助匯出」。

**IDOR：** `_get_parent_student_ids` 自然處理（已 redacted 的 Guardian.user_id NULL → 拿不到任何 student → 回空 students[]，不報錯）。

---

## 8. 前端（`MeView.vue`）

```vue
<!-- 在 NotificationPrefs row 下方加 -->
<ContactBookCard>
  <button class="row-button" @click="showExportDialog = true">
    <Icon name="download" />
    <span>下載我的個人資料</span>
    <Icon name="chevron-right" class="muted" />
  </button>
</ContactBookCard>

<AppModal v-model="showExportDialog" title="下載個人資料">
  <p>將下載您與孩子在園所的所有紀錄（JSON 格式）。</p>
  <ul class="muted small">
    <li>包含聯絡簿、出席、請假、繳費、投藥、相片連結、訊息、成長報告</li>
    <li>每小時限下載 1 次</li>
    <li>檔案上限 50MB</li>
  </ul>
  <template #actions>
    <button @click="downloadExport" :disabled="downloading">
      {{ downloading ? '下載中…' : '確認下載' }}
    </button>
  </template>
</AppModal>
```

```ts
// composables/useDataExport.ts
import { ref } from 'vue'
import { apiParent } from '@/parent/api'

export function useDataExport() {
  const downloading = ref(false)

  async function downloadExport() {
    downloading.value = true
    try {
      const resp = await apiParent.get('/me/data-export', { responseType: 'blob' })
      const url = URL.createObjectURL(resp.data)
      const a = document.createElement('a')
      a.href = url
      a.download = resp.headers['content-disposition']
        ?.match(/filename="?([^";]+)"?/)?.[1]
        ?? 'ivy_data_export.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch (err: any) {
      if (err.response?.status === 429) {
        // 顯示「請稍後再試」
      } else if (err.response?.status === 413) {
        // 顯示「資料量過大，請聯絡園所」
      } else {
        throw err
      }
    } finally {
      downloading.value = false
    }
  }

  return { downloading, downloadExport }
}
```

---

## 9. 測試覆蓋

**後端（pytest）：**

| 測試檔 | 範圍 |
|--------|------|
| `tests/test_student_lifecycle_helper.py` | set_lifecycle_status: 非終態→終態寫戳記 / 終態→非終態清戳記 / 終態→終態不動 / 同狀態 no-op / audit_log 寫對 |
| `tests/test_pii_retention_gc.py` | 12 個月內不抹 / 滿 365 天才抹 / dry-run 不寫 DB / SKIP LOCKED 多 worker / 已 pii_redacted_at 不重複 / Guardian.deleted_at 也跳過 / audit_log 寫對且不含 PII / user_id 解綁後 _get_parent_student_ids 回空 |
| `tests/test_data_export.py` | JSON shape / Content-Disposition header / rate-limit 觸發 429 / 50MB 觸發 413 / IDOR（別人 user 拿不到）/ 已 redacted 家長拿到空 students[] / 終態學生資料仍包含（retention 期內）|
| `tests/test_alembic_pretent001.py` | up 加欄位+index+backfill / backfill 從 audit_log 反推 / backfill 沒 audit_log 用 updated_at / down 完全乾淨 |
| `tests/test_lifecycle_helper_caller_audit.py` | grep 確認沒有直接 `student.lifecycle_status =` 殘留 caller（用 AST 掃 ivy-backend/api/ + services/）|

**前端（vitest）：**

| 測試檔 | 範圍 |
|--------|------|
| `parent/components/MeView.test.js` 擴 | export button render / dialog 開關 / 429 / 413 / 成功下載 blob |
| `parent/composables/useDataExport.test.ts` | filename parse / blob URL lifecycle / error handling |

**Smoke / 整合：**
- `e2e/` 不加（家長 LIFF mock smoke 是 follow-up，本案不擴）

---

## 10. CLAUDE.md 更新

**`ivy-backend/CLAUDE.md`：** 加「PII Retention 政策」章節：
- 365 天 default、ENV 可調
- Guardian 範圍、不動 Student
- 復學自動取消 retention
- dry-run 預設開、上線後人工關閉

**workspace `CLAUDE.md`：** 加 retention 跨端注意：
- data-export 走 LIFF auth，不算 admin endpoint
- 已 redacted 家長 LIFF 仍能登入但 portal 空白（by design）

---

## 11. 部署順序

1. **Migration**：`alembic upgrade heads` → 加欄位 + backfill 現有終態學生 `terminal_entered_at`
2. **後端 deploy**：含 set_lifecycle_status helper、scheduler 新檔（**ENV 預設 disabled + dry-run**）、data-export endpoint
3. **OpenAPI codegen**：`python scripts/dump_openapi.py` + 前端 `npm run gen:api`
4. **前端 deploy**：MeView export 按鈕
5. **第一輪 GC dry-run**：user 把 `PII_RETENTION_GC_DRY_RUN=1` 維持、`PII_RETENTION_GC_DISABLED=0` 開啟，看 log 列出將被 redact 的 Guardian 清單
6. **User 人工確認** backfill 結果合理（特別是 audit_log 反推失準的 case 是否需要手動修 terminal_entered_at）
7. **正式啟用**：`PII_RETENTION_GC_DRY_RUN=0`、`PII_RETENTION_GC_DISABLED=0`，每日 GC 開始抹

---

## 12. 風險與緩解

| 風險 | 緩解 |
|------|------|
| backfill `terminal_entered_at` 失準（audit_log 沒記 / updated_at 被其他變更動到）導致提前 redact | 三層防護：(1) backfill 從 audit_log 找精準時間 (2) dry-run 預設開 (3) ENV 預設 disabled，user 親自 review log 後再啟用 |
| 復學家長被誤抹 | 復學 → set_lifecycle_status active → terminal_entered_at NULL，GC SQL 自然不抓 |
| 同一 User 綁多 student，一胎畢業另一胎在學 | GC 針對 Guardian row（非 User），只抹該胎 Guardian；其他在學 Guardian.user_id 仍綁同 User，LIFF 仍能登入看在學那胎 |
| Data-export size > 50MB（極端：園所紀錄 6 年 + 大量照片連結）| 回 413 + 提示聯絡園所；後續可加非同步 zip 版本（follow-up） |
| GC scheduler 在 multi-worker 部署重複跑 | `FOR UPDATE SKIP LOCKED`；env `PII_RETENTION_GC_DISABLED` 可在 non-leader worker 設 1 |
| 法務認定 365 天太長/太短 | ENV `PII_RETENTION_TERMINAL_DAYS` 可調，無需改 code |
| `pii_redact` 動作未來如需 reverse | 不可逆（PII 已被 UPDATE 覆蓋）；audit_log 留 student_id + lifecycle + 日期供事後查證但無法復原 phone/email |

---

## 13. 不在本 spec 範圍（明確排除）

- Student 表 PII（姓名/生日/身分證）retention——學籍學術紀錄保存政策需與教育法規一併評估，本案不動
- medication.notes / leave_request.reason / message.content 等高敏感內容 retention（user Q2 選最小範圍）
- 凍結期（30 天反悔窗口）——user Q3 選不特殊處理
- 非同步生成 zip 走 LINE 推播下載 URL——本案同步 JSON 即可
- 招生 funnel prospect/visited 階段的 PII retention（招生階段 PII 是另一話題）
- 教師端、admin 端的 PII export
- Sentry PII denylist 異動——phone/email/name 已在 denylist

---

## 14. Spec → Plan 銜接

下一步：用 superpowers:writing-plans 把本 spec 拆成可執行的 task 序列。預期 task 數約 **12-15**：

1. Alembic migration pretent001（schema + backfill）
2. `config/scheduler.py` 三個新 env field
3. `utils/student_lifecycle.set_lifecycle_status` helper + test
4. Grep + 改造所有直接寫 `lifecycle_status =` 的 caller（含 graduation_scheduler / student_enrollment）
5. `services/pii_retention_scheduler.py` + test
6. `main.py` 啟動 scheduler hook
7. `api/parent_portal/data_export.py` endpoint + test
8. 註冊 router 到 `api/parent_portal/__init__.py`
9. OpenAPI dump + 前端 codegen
10. 前端 `composables/useDataExport.ts`
11. 前端 MeView 加 export button + dialog + test
12. CLAUDE.md 更新（兩處）
13. 整合驗證（dev server + LIFF）
14. 前後端各 commit
