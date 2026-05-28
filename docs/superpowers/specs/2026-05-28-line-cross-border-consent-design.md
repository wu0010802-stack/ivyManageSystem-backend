# Spec E: LINE 推播跨境合規 (#6)

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：🔴 P0 #6 — LINE 推播訊息含 PII，跨境傳輸未告知
**對應 spec 系列**：A (限流) ✅ / B (CSRF) ✅ / D (audit append-only) ✅ / C (Logger PII) ✅ / **E (LINE 跨境)** / F (staff refresh)

---

## 1. Why

### 1.1 攻擊面 + 法規

`services/line_service.py:127 build_activity_waitlist_promoted_message` 「🎨 才藝候補升位通知 學生：{student_name} 課程：{course_name}」、`:187 build_dismissal_message` 「【接送通知】學生：{student_name} 班級：{classroom_name}」 等 5 個 build_*_message 函式直接 inline student_name + classroom_name + course_name 到推播文字，經 `https://api.line.me/v2/bot/message/push` 傳到 LINE 日韓 server。

**法規違反**：
- 個資法 §21（跨境傳輸限制）— 對未告知第三國的資料傳遞需事先告知 + 同意
- 個資法 §8（告知義務含「國際傳遞之事實」）— 蒐集時未告知 LINE 日韓 server
- GDPR Art. 44-49（第三國資料傳輸）— 若涉歐盟客戶需 adequacy decision / SCC

家長綁定 LINE 時無跨境傳輸告知 + 同意流程；推播訊息 by-design 含 student PII。

### 1.2 三層解法（user 拍板）

| Phase | 範圍 | 本 spec 含 | 工時 |
|-------|------|-----------|------|
| **Phase 1** (tech) | Guardian / User consent flag + push gate + 訊息去識別化 | ✅ | 6-8h (BE) |
| **Phase 2** (legal) | 隱私政策文補充跨境告知 LINE/Supabase/Sentry/Cloudflare R2 + 國別 | ❌ **out-of-scope** (user 自出文) | — |
| **Phase 3** (UX) | LIFF 綁定加 consent checkbox + Settings consent toggle | ✅ | 4-6h (FE) |

**Phase 2 由 user 自出隱私政策文後加進前端**（user 拍板）；本 spec 不寫 legal text。

### 1.3 既有 line binding 架構

- `models/auth.py:62 User.line_user_id` — User 表已有 LINE binding column
- `services/line_service.py` 有 `push_text_to_user(user_id, text)` / `push_flex_to_user(user_id, ...)` 等多個 push 端點
- Guardian 表無 LINE 欄位（一 user 對應 1 LINE binding，跨多 student）

`line_push_consent` 加在 **User 表**（不在 Guardian 表）— 一個 LINE 帳號 1 個 consent。

---

## 2. Goals / Non-goals

### Goals
- (G1) **BE**: User 表加 `line_push_consent: Boolean default False`（opt-in）+ alembic migration
- (G2) **BE**: `services/line_service.py` 所有 push_*_to_user 在實際 call LINE API 前 query User WHERE line_user_id 找對應 user → check line_push_consent；False 則 skip + log warning (不 raise)
- (G3) **BE**: 5 個含 PII 的 build_*_message 改去識別化：
  - `build_activity_waitlist_promoted_message` student_name → 「您的孩子」
  - `build_activity_waitlist_promotion_reminder_message` 同上
  - `build_activity_waitlist_promotion_expired_message` 同上
  - `build_activity_waitlist_final_reminder_message` 同上
  - `build_dismissal_message` student_name + classroom_name → 「您的孩子已可接送」
  - 文字仍含 course_name / 提示資訊（非 PII），加 deep link 到家長 App 看詳情
- (G4) **BE**: deep link template `https://{frontend_host}/portal/notifications/{notification_id}`（已 mask PII，家長 LIFF 認證後可看詳情）
- (G5) **FE (ivy-frontend)**: LIFF 綁定流程加 consent checkbox + 同意說明（user 補隱私政策文後填入）
- (G6) **FE (ivy-frontend)**: PortalSettingsView 加 LINE 推播 toggle (call PATCH /api/parent/me/line-consent)
- (G7) 零回歸：既有 pytest baseline + 新 5-7 test 全綠
- (G8) **既有 user 全部預設 False**: migration backfill 不動 existing user 的 line_push_consent（保持 False 直到家長 explicit opt-in via LIFF UI）

### Non-goals
- 不在本 spec 內寫隱私政策法律文（user 自出）
- 不改 LINE webhook callback（input 端不變，只改 output push）
- 不對 broadcast_* 群播訊息加 consent gate（broadcast 是管理員操作，receivers 是公開 LINE 帳號群，非個人 PII context）
- 不對其他第三方 service (Supabase / Sentry / Cloudflare R2) 加 consent flag（屬 Phase 2 legal 範圍）
- 不引入 LINE messaging API token rotation / 限流（本 spec 不涉）
- 不在本 spec 內處理 Spec F (staff refresh)

---

## 3. Architecture

### 3.1 PR 結構（兩 PR 同 spec）

| Repo | Branch | Commit 結構 | 工時 |
|------|--------|------------|------|
| **ivy-backend** | `feat/line-cross-border-consent-2026-05-28-backend` | 3 commits (migration / consent gate + 訊息去識別化 / pytest) | 6-8h |
| **ivy-frontend** | `feat/line-cross-border-consent-2026-05-28-frontend` | 2 commits (LIFF bind consent / Settings toggle) | 4-6h |

兩 PR cross-reference spec.md，user 必先 merge backend (consent flag 存在) 才能 merge frontend。

### 3.2 Migration（PR-E-BE-C1）

新檔 `alembic/versions/YYYYMMDD_lncon01_user_line_push_consent.py`：

```python
"""user_line_push_consent: 家長 LINE 推播跨境同意 flag

Revision ID: lncon01
Revises: <當前 head>
Create Date: 2026-05-28

Why:
    Audit P0 #6 / Spec E：LINE 推播訊息含 PII 經 LINE 日韓 server 跨境
    傳輸，原無 consent gate。加 User.line_push_consent 預設 False (opt-in)，
    line_service push_*_to_user 前 check consent，未同意 skip + log warning。

    Existing user 不 backfill True（保持 opt-in 原則）；需家長透過 LIFF UI
    explicit 勾選同意才開始收 LINE push。

    Refs: 個資法 §8 §21、GDPR Art. 44-49
"""

import sqlalchemy as sa
from alembic import op

revision = "lncon01"
down_revision = "<plan stage 確認 head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "line_push_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="LINE 推播跨境傳輸同意（P0 #6 / Spec E）；opt-in 預設 False",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "line_push_consent")
```

### 3.3 Consent gate + 訊息去識別化（PR-E-BE-C2）

`services/line_service.py` 新加 helper：

```python
def _check_line_push_consent(line_user_id: str) -> bool:
    """Query User WHERE line_user_id, return line_push_consent value。
    
    未綁定 LINE user / consent False → return False (skip push)。
    DB 異常 → return False (fail-closed: 跨境合規不可放行)。
    """
    from models.auth import User
    from models.database import session_scope
    try:
        with session_scope() as session:
            user = session.query(User).filter(User.line_user_id == line_user_id).first()
            if not user:
                return False
            return bool(user.line_push_consent)
    except Exception as e:
        logger.warning("check_line_push_consent failed for %s: %s", line_user_id, e)
        return False  # fail-closed
```

所有 push_*_to_user 在 LINE API call 前 gate：

```python
def push_text_to_user(self, user_id: str, text: str) -> None:
    if not _check_line_push_consent(user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", user_id)
        return
    # ... 既有 LINE API call ...

def push_flex_to_user(self, user_id: str, ...):
    if not _check_line_push_consent(user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", user_id)
        return
    # ... 既有 ...

def push_to_user(self, line_user_id: str, text: str) -> bool:
    if not _check_line_push_consent(line_user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", line_user_id)
        return False  # 不算 fail，是 skip
    # ... 既有 ...
```

5 個 build_*_message 改去識別化：

```python
# Before
def build_activity_waitlist_promoted_message(student_name, course_name, deadline=None):
    base = f"🎨 才藝候補升位通知\n學生：{student_name}\n課程：{course_name}\n"
    ...

# After
def build_activity_waitlist_promoted_message(
    course_name: str,
    deadline: Optional[datetime] = None,
    detail_url: Optional[str] = None,   # frontend deep link
) -> str:
    base = f"🎨 才藝候補升位通知\n您的孩子課程：{course_name}\n"
    if detail_url:
        base += f"詳情：{detail_url}\n"
    ...
```

`student_name` 參數從簽章移除（caller 不再傳）。`classroom_name` 改用「您的孩子已可接送」（dismissal message）。

Caller 端 (api/activity/* / scheduler / etc) 需同步改 — plan stage `grep -n "build_activity_waitlist_promoted_message(" --include="*.py"` 找所有 caller 並改簽章。

### 3.4 Deep link 端點（PR-E-BE-C2 同 commit）

新加 endpoint `api/parent_portal/notifications.py`：

```python
@router.get("/me/notifications/{notification_id}", response_model=NotificationDetailOut)
def get_parent_notification_detail(
    notification_id: int,
    current_user: dict = Depends(get_current_parent_user),
):
    """家長 LIFF 認證後看詳情（含 student_name / classroom_name / course_name 完整資訊）。
    LINE 推播訊息只給 deep link，敏感資料留 own server。
    """
    # 既有 notification 模型 + filter by current_user.student_id
    ...
```

`detail_url` 在 build_*_message 前由 caller 組裝：`f"{settings.network.frontend_url}/portal/notifications/{notification.id}"`

### 3.5 BE pytest (PR-E-BE-C3)

新 `tests/test_line_consent_gate.py`：

1. `test_push_text_to_user_skipped_when_no_consent` — consent=False → skip + log warning
2. `test_push_text_to_user_called_when_consent_true` — consent=True → 實際 call LINE
3. `test_push_when_user_not_bound_to_line` — line_user_id 找不到 user → skip (fail-closed)
4. `test_check_line_push_consent_db_error_fails_closed` — DB raise → return False
5. `test_build_activity_waitlist_promoted_message_no_student_name` — output 不含 student_name
6. `test_build_dismissal_message_no_classroom_name` — output 不含 classroom_name

### 3.6 FE (ivy-frontend) PR-E-FE 設計

**Files:**
- `src/views/parent/PortalLineBindView.vue` — LIFF 綁定加 consent checkbox + 同意說明區
- `src/views/parent/PortalSettingsView.vue` — 新 LINE 推播 toggle row
- `src/api/parentSettings.ts` (or 同檔) — call `PATCH /api/parent/me/line-consent`

**UX 流程**：
1. 家長首次 LIFF login bind → 顯示同意說明（user 補隱私政策文後填入）+ checkbox「我同意 LINE 推播跨境傳輸（傳送至 LINE Corp 日韓 server）」
2. 未勾選 → 仍可 bind 但 line_push_consent=False（後續 push 全 skip）
3. 已 bind 後可在 Settings 隨時 toggle consent on/off

**BE 配套 endpoint**:
```python
@router.patch("/me/line-consent")
def update_parent_line_consent(
    data: ConsentRequest,  # {consent: bool}
    current_user: dict = Depends(get_current_parent_user),
):
    """家長 toggle LINE 推播 consent。"""
    # update User.line_push_consent + write_audit_log
```

### 3.7 Sentry / Supabase / Cloudflare R2 跨境

本 spec **不**處理。屬 Phase 2 legal 範圍：
- Sentry: prod 部署在歐美/亞洲 region (依 Sentry plan)
- Supabase: prod 部署 region 依 setup（可能 AWS 全球節點）
- Cloudflare R2: 全球 CDN
- LINE: 日韓 server

Phase 2 user 出隱私政策文時逐一列出國別 + 傳輸目的（Sentry 錯誤監控、Supabase storage、R2 backup、LINE 推播）。

---

## 4. 測試計畫

**BE pytest (5-7 new tests)** in `tests/test_line_consent_gate.py`:
- 4 consent gate tests (skip / pass / not bound / db error)
- 2 message redaction tests (no student_name / no classroom_name)
- 1 deep link endpoint test (auth required + filter by current student)

**FE vitest (2 new tests)** in `tests/parent/portal-line-bind.test.ts`:
- consent checkbox required for bind workflow
- Settings toggle persists to PATCH endpoint

**回歸**：兩 repo 既有 baseline 全綠 + 新 test 全綠。

---

## 5. Roll-out

### 5.1 部署步驟

1. **BE PR 先 merge** + alembic upgrade (lncon01 加 column default False)
2. 後端 service 重啟 → consent gate 立刻生效
   - **副作用**: prod 既有家長預設 False → **立刻不收 LINE push**
   - 必須先溝通：寄信通知家長「將推出 LINE 推播同意機制，請於 X 日內透過家長 App 重新同意」
3. **FE PR merge** + 部署
4. 家長透過 LIFF 重新 bind 或 Settings 勾選同意 → 開始收 LINE push
5. **Phase 2 法律文 user 出** → 補進 LIFF bind page 同意說明

### 5.2 回退方案

- Revert BE PR + alembic downgrade (drop line_push_consent column) → 回到原行為 (所有 push 都送)
- 或臨時 patch: `UPDATE users SET line_push_consent = true WHERE line_user_id IS NOT NULL` 全 backfill True (緊急救火，不建議常態)

### 5.3 監控指標

7 天觀察：
- `LINE push skip (no consent)` log 量：應 100% (家長沒重新同意) → 隨家長 opt-in 下降
- LINE messaging API call rate 應暫時 drop (按 consent rate 比例)
- 家長 LIFF bind page 跳出率（若太高表示同意說明文太勸退）

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| 既有家長沒重新同意 → 完全收不到 LINE push | 業務中斷 (接送/才藝候補通知無法送) | spec §5.1 必先寄信通知 + 給 grace period；FE Settings 顯眼 toggle |
| build_*_message 簽章改動 → caller 端漏改 | runtime TypeError | plan stage `grep -n "build_activity_waitlist_promoted_message(" --include="*.py"` 找所有 caller 同步改 |
| deep link 失效（notification 已 GC / 家長未 LIFF login） | 家長收訊息但點不開詳情 | deep link 端點對未認證返回引導 LIFF login；過期 notification 顯示「通知已過期」 |
| Phase 2 legal 文未出 → FE consent checkbox 同意說明空白 | 形同 dark pattern 未告知 | FE PR merge 前 user 必出最少版本同意說明文（可後續法律審完替換）|
| `line_user_id` 在 Parent 端是不同 user table (parent_users) 而非 staff User table | _check_line_push_consent 找錯表 | plan stage verify `line_user_id` 在 User 表還是 parent_users 表（spec §1.3 假設在 User 表，待 confirm） |

---

## 7. Out of scope

- Phase 2 隱私政策法律文（user 自出）
- Sentry / Supabase / Cloudflare R2 跨境告知 mechanism（屬 Phase 2 範圍）
- 不改 LINE webhook callback (input)
- 不改 broadcast_* 群播
- Spec F (staff refresh) 為獨立 spec
- 不引入 LINE messaging API 限流

---

## 8. 驗收 checklist

PR 合併 + deploy 後 USER 手動驗證：

- [ ] `alembic upgrade head` 跑 lncon01 無錯
- [ ] `SELECT line_push_consent, COUNT(*) FROM users GROUP BY line_push_consent` 確認既有家長 default False
- [ ] 任一才藝候補升位 / 接送通知 trigger → audit_logs 內看到 skip 紀錄 (家長沒 opt-in)
- [ ] 寄信通知家長
- [ ] FE LIFF bind page 顯示 consent checkbox + 同意說明（待 Phase 2 法律文）
- [ ] 家長 LIFF login → 看到「請同意 LINE 推播跨境告知」說明
- [ ] 勾選後重 trigger 推播 → 收到 LINE 訊息 (僅含「您的孩子」+ deep link，無 student_name)
- [ ] 點 deep link → LIFF 認證後看到 student_name 詳情
- [ ] Settings 取消 toggle → 不再收推播
- [ ] 抽 1 個推播 LINE app 截圖確認無學生姓名/班級/接送資訊洩漏

---

## 9. 後續 follow-up

- Phase 2: user 出隱私政策文後加進 LIFF bind page + Settings 同意說明
- Sentry / Supabase / Cloudflare R2 跨境告知（個別 spec）
- 家長端 audit_log 看自己同意紀錄歷史（GDPR / 個資法 §10 查閱權）
- consent 改動發訊息給 staff (避免家長靜默 opt-out 業務通知漏接)
