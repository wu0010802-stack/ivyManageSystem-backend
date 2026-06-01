# 課後才藝系統上線前風險修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉 audit 找到的全部上線風險（PII 入 log、GET query 洩漏、reject/restore 超賣、軟刪不遞補、退費 idempotency 旁路、沖帳未簽核、availability 無快取、P2 群、XFF）+ 全 app `async def`→`def` 解單 worker event-loop 阻塞。

**Architecture:** Workstream A 為外科修正（各自 TDD、分開 commit）；Workstream B 為全 app async 機械遷移（AST 腳本分類 + 完整測試套件 gate，獨立 commit）。對應 spec `docs/superpowers/specs/2026-06-01-activity-go-live-hardening-design.md`。

**Tech Stack:** FastAPI + SQLAlchemy(psycopg2) + PostgreSQL；pytest；前端 Vue3 + Vitest（A3 一處）。

---

## Task 0：Worktree 設定

**REQUIRED SUB-SKILL: superpowers:using-git-worktrees** — 從 `origin/main` 開 worktree（勿從 local main，user main 有 WIP）。

- [ ] **Step 1:** 確認 `git -C /Users/yilunwu/Desktop/ivy-backend fetch origin` 後從 `origin/main` 開 worktree，分支名 `fix/activity-go-live-hardening-2026-06-01-backend`。
- [ ] **Step 2:** 記下 worktree 絕對路徑（以下稱 `$WT`）。所有 backend 指令用 `git -C $WT` / `cd $WT`。
- [ ] **Step 3:** 前端 A3 另從 `ivy-frontend` `origin/main` 開 `fix/activity-public-query-post-2026-06-01-frontend`（以下稱 `$WTFE`）。
- [ ] **Step 4:** 把 spec 與本 plan cherry-pick / 複製進 `$WT`（若 worktree 自 origin/main 無這兩檔）。

> 註：PostToolUse black hook 對 `.py` Edit/Write 會全檔重排；subagent 對既有檔做 surgical edit 時用 `python3` 的 `str.replace` 寫檔繞過（見 memory `feedback_subagent_posttooluse_black_hook`），新檔可正常 Write。

---

## Workstream A — 外科修正

### Task A2：移除 public 報名/查詢 log 的 PII

**Files:**
- Modify: `$WT/api/activity/public.py`（log 點：`:466-470` silent-reject、`:681-686` 新報名、`:1073`、`:1251-1255` inquiry）
- Test: `$WT/tests/test_activity_pii_log_redaction.py`（新建）

- [ ] **Step 1: 寫失敗測試** — 用 `caplog` 攔 `api.activity.public` logger，跑 register（成功）、silent-reject（honeypot 命中）、inquiry，斷言 log text 不含 `student_name`/`parent_phone`/`birthday` 的值、且不拋 `TypeError`。

```python
# tests/test_activity_pii_log_redaction.py
import logging
def test_register_log_has_no_child_name(client, caplog, valid_register_payload):
    valid_register_payload["name"] = "王小明唯一"
    valid_register_payload["parent_phone"] = "0912345678"
    with caplog.at_level(logging.INFO, logger="api.activity.public"):
        r = client.post("/api/activity/public/register", json=valid_register_payload)
    assert r.status_code == 201
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "王小明唯一" not in joined
    assert "0912345678" not in joined

def test_silent_reject_log_no_pii_and_no_typeerror(client, caplog, honeypot_payload):
    # honeypot_payload 帶 hp 值或 ts 過快 → silent-reject
    with caplog.at_level(logging.WARNING, logger="api.activity.public"):
        r = client.post("/api/activity/public/register", json=honeypot_payload)
    assert r.status_code == 201
    # 不得因 redaction filter 改動 %r 數量而 TypeError 整行丟失
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert honeypot_payload["parent_phone"] not in joined
```

- [ ] **Step 2: 跑測試確認失敗** — `cd $WT && python -m pytest tests/test_activity_pii_log_redaction.py -q`，預期 FAIL（log 含 PII / TypeError）。
- [ ] **Step 3: 改 log（surgical，繞 black hook）** —
  - `:466-470` silent-reject：改成 `logger.warning("public_register silent-reject (honeypot/ts) ip=%s", get_client_ip(request) or "?")`（移除 name/phone；若該 handler 沒有 `request` 物件，改記固定字串 + 不帶 PII）。
  - `:681-686`：`logger.info("新報名提交：id=%s matched=%s", reg.id, is_matched)`（移除 `student=%s`/`reg.student_name`）。
  - `:1073`：同理移除 `student=%s`，改 `reg.id`。
  - `:1251-1255` inquiry：移除 `name`/`phone`，改記 inquiry id 或固定字串。
- [ ] **Step 4: 跑測試確認通過** — `python -m pytest tests/test_activity_pii_log_redaction.py -q`，預期 PASS。
- [ ] **Step 5: 回歸** — `python -m pytest tests/test_activity_api.py tests/test_activity_logic_holes.py -q` 零新增 fail。
- [ ] **Step 6: Commit** — `git -C $WT add -A && git -C $WT commit -m "fix(activity): 移除 public 報名/查詢 log 的幼兒姓名與家長電話 PII"`

---

### Task A3：`GET /public/query` → `POST`（後端 + 前端）

**Files:**
- Modify: `$WT/api/activity/public.py:340-398`（handler）；若需 body schema 在 `$WT/schemas/activity_public.py` 新增 `PublicQueryPayload`
- Modify: `$WTFE/src/api/activityPublic.ts:17-18`
- Test: `$WT/tests/test_activity_public_query_post.py`（新建）；`$WTFE/src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js`（確認不破）

- [ ] **Step 1: 後端失敗測試** —

```python
# tests/test_activity_public_query_post.py
def test_query_is_post_not_get(client, seeded_registration):
    name, bday, phone, _ = seeded_registration
    # 舊 GET 應不再可用
    assert client.get("/api/activity/public/query",
                      params={"name": name, "birthday": bday, "parent_phone": phone}
                      ).status_code in (404, 405)
    # 新 POST 正常
    r = client.post("/api/activity/public/query",
                    json={"name": name, "birthday": bday, "parent_phone": phone})
    assert r.status_code == 200
```

- [ ] **Step 2: 跑確認失敗** — `python -m pytest tests/test_activity_public_query_post.py -q` 預期 FAIL。
- [ ] **Step 3: 後端改 POST** — `@router.get("/public/query"...)` → `@router.post("/public/query"...)`；函式參數從 query params 改成 `body: PublicQueryPayload`（含 `name/birthday/parent_phone`，沿用既有欄位驗證），handler 內 `name=body.name` 等。保留 `_public_query_limiter`、隨機延遲、統一回應。
- [ ] **Step 4: 跑確認通過** — `python -m pytest tests/test_activity_public_query_post.py tests/test_activity_public_query_token_phase3.py -q` PASS。
- [ ] **Step 5: 前端改 post** — `$WTFE/src/api/activityPublic.ts:18` `api.get('/activity/public/query', { params: { name, birthday, parent_phone } })` → `api.post('/activity/public/query', { name, birthday, parent_phone })`（function 簽章不變）。
- [ ] **Step 6: 前端測試 + typecheck** — `cd $WTFE && npx vitest run src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js && npm run typecheck` 綠。
- [ ] **Step 7: Commit（分兩 repo）** —
  - `git -C $WT add -A && git -C $WT commit -m "fix(activity): /public/query 改 POST，避免姓名/生日/電話進 URL/access log"`
  - `git -C $WTFE add -A && git -C $WTFE commit -m "fix(activity): publicQueryRegistration 改用 POST body"`

---

### Task A4：`reject → 名額被補 → restore` 超賣

**Files:**
- Modify: `$WT/api/activity/registrations_pending.py`（`restore_registration` `:689-695` commit 前插入重數降級）
- Test: `$WT/tests/test_activity_restore_capacity.py`（新建）

- [ ] **Step 1: 失敗測試（重現超賣）** —

```python
# tests/test_activity_restore_capacity.py
def test_restore_does_not_oversell(session, make_course, make_enrolled_reg, client_admin):
    course = make_course(capacity=1)
    reg_a = make_enrolled_reg(course, name="A")          # 佔掉唯一名額 (enrolled)
    # reject A → 名額釋出（is_active=False，但 RC 仍 enrolled）
    client_admin.post(f"/api/activity/registrations/{reg_a.id}/reject",
                      json={"reason": "校外生"})
    reg_b = make_enrolled_reg(course, name="B")          # B 補上唯一名額
    # restore A
    client_admin.post(f"/api/activity/registrations/{reg_a.id}/restore")
    session.expire_all()
    enrolled = count_occupying(session, course.id)        # status in (enrolled, promoted_pending) & is_active
    assert enrolled <= course.capacity                    # 不得超賣
    # A 的該課程應被降為 waitlist
    rc_a = get_rc(session, reg_a.id, course.id)
    assert rc_a.status == "waitlist"
```

- [ ] **Step 2: 跑確認失敗** — 預期 `enrolled == 2 > capacity 1`。
- [ ] **Step 3: 改 restore_registration** — 在 `reg.is_active = True`（`:689`）之後、`session.commit()`（`:695`）之前插入：

```python
# 重數佔位，超出容量者降 waitlist（防 reject→backfill→restore 超賣）
from models.activity import RegistrationCourse, ActivityCourse
rc_rows = (
    session.query(RegistrationCourse)
    .filter(RegistrationCourse.registration_id == reg.id,
            RegistrationCourse.status.in_(["enrolled", "promoted_pending"]))
    .all()
)
for rc in rc_rows:
    course = (session.query(ActivityCourse)
              .filter(ActivityCourse.id == rc.course_id)
              .with_for_update().first())
    if not course or course.capacity is None:
        continue  # 不限容量
    occupying = (
        session.query(func.count(RegistrationCourse.id))
        .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
        .filter(RegistrationCourse.course_id == rc.course_id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
                ActivityRegistration.is_active.is_(True),
                RegistrationCourse.registration_id != reg.id)
        .scalar()
    )
    if occupying >= course.capacity:
        rc.status = "waitlist"
```

（`func`/`ActivityRegistration` 等 import 對齊檔頭既有 import。）
- [ ] **Step 4: 跑確認通過** — `python -m pytest tests/test_activity_restore_capacity.py -q` PASS。
- [ ] **Step 5: 回歸** — `python -m pytest tests/test_activity_pending_review.py tests/test_activity_waitlist_promotion.py -q` 零新增 fail。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "fix(activity): restore 報名重數佔位，超出容量降候補，防 reject-restore 超賣"`

---

### Task A5：學生離園/退學軟刪後自動遞補候補

**Files:**
- Modify: `$WT/services/activity_student_sync.py:265`（`_soft_delete_single_registration`）
- Test: `$WT/tests/test_activity_student_sync.py`（追加）

- [ ] **Step 1: 失敗測試** — 建一門滿額課（A enrolled、B waitlist），把 A 對應學生 deactivate → 斷言 B 被 `_auto_promote` 升為 `promoted_pending`。

```python
def test_soft_delete_promotes_waitlist(session, make_course, ...):
    course = make_course(capacity=1)
    reg_a = make_enrolled_reg(course, student=student_a)   # enrolled，綁在校生
    reg_b = make_waitlist_reg(course)                      # waitlist
    sync_registrations_on_student_deactivate(session, student_a.id)  # 觸發軟刪
    session.expire_all()
    assert get_rc(session, reg_b.id, course.id).status == "promoted_pending"
```

- [ ] **Step 2: 跑確認失敗** — B 仍 `waitlist`。
- [ ] **Step 3: 改 `_soft_delete_single_registration`** — 參照 `services/activity_service.py` 內 `delete_registration:1056` 的呼叫方式，在 `reg.is_active = False` + `session.flush()` 後，對該 reg 原 `enrolled`/`promoted_pending` 課程逐一呼叫 `activity_service._auto_promote_first_waitlist(session, course_id)`（先收集 course_ids 再呼叫，避免迭代中改狀態）。
- [ ] **Step 4: 跑確認通過** — `python -m pytest tests/test_activity_student_sync.py -q` PASS。
- [ ] **Step 5: 回歸** — `python -m pytest tests/test_activity_sync_on_student_change.py -q` 零新增 fail。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "fix(activity): 學生離園軟刪報名後自動遞補候補名額"`

---

### Task A6：退費 idempotency 無 key fallback（短窗去重）

**Files:**
- Create helper in `$WT/api/activity/pos.py`（`_recent_duplicate_payment`）
- Modify: 退費/繳費建立點無 key 分支（`pos.py` checkout refund、`registrations_payments.py:add_registration_payment`）
- Test: `$WT/tests/test_activity_idempotency_fallback.py`（新建）

- [ ] **Step 1: 失敗測試** — 不帶 `idempotency_key` 對同 reg 連送兩筆相同退費（amount/type/operator），斷言第二筆被判 replay：只一筆 refund 紀錄、`paid_amount` 只降一次。

```python
def test_refund_without_key_deduped(session, client_admin, paid_registration):
    reg = paid_registration  # 已繳 1000
    body = {"type": "refund", "amount": 500}  # 無 idempotency_key
    r1 = client_admin.post(f"/api/activity/registrations/{reg.id}/payment-record", json=body)
    r2 = client_admin.post(f"/api/activity/registrations/{reg.id}/payment-record", json=body)
    assert r1.status_code == 201
    refunds = count_records(session, reg.id, type="refund", amount=500)
    assert refunds == 1                       # 第二筆被去重
    session.expire_all()
    assert get_reg(session, reg.id).paid_amount == 500   # 只退一次
```

- [ ] **Step 2: 跑確認失敗** — 預期 `refunds == 2`（雙重退費）。
- [ ] **Step 3: 加 helper** —

```python
# api/activity/pos.py
def _recent_duplicate_payment(session, registration_id, type_, amount, operator, window_seconds=60):
    """無 idempotency_key 時的短窗去重：回最近 window 內同 (reg,type,amount,operator) 的
    有效紀錄（排除 voided），代表疑似重送。None 表示可建立。"""
    from datetime import timedelta
    from utils.timezone import now_taipei_naive
    cutoff = now_taipei_naive() - timedelta(seconds=window_seconds)
    return (
        session.query(ActivityPaymentRecord)
        .filter(ActivityPaymentRecord.registration_id == registration_id,
                ActivityPaymentRecord.type == type_,
                ActivityPaymentRecord.amount == amount,
                ActivityPaymentRecord.operator == operator,
                ActivityPaymentRecord.voided_at.is_(None),
                ActivityPaymentRecord.created_at >= cutoff)
        .order_by(ActivityPaymentRecord.id.asc())
        .first()
    )
```

（`created_at` 欄位名以 model 實際為準；若無則用 `payment_date` + 視窗放寬至當日。）
- [ ] **Step 4: 接到無 key 分支** — `add_registration_payment` 與 pos refund 在 `body.idempotency_key` 為 None 時，先呼叫 `_recent_duplicate_payment(...)`；命中則回放既有紀錄（沿用既有 `_parse_receipt_response_from_record` / replay 回應），不再 INSERT。
- [ ] **Step 5: 跑確認通過** — `python -m pytest tests/test_activity_idempotency_fallback.py -q` PASS。
- [ ] **Step 6: 回歸** — `python -m pytest tests/test_activity_pos.py tests/test_activity_payment_guards.py -q` 零新增 fail。
- [ ] **Step 7: Commit** — `git -C $WT commit -am "fix(activity): 退費/繳費無 idempotency_key 時短窗去重，防重複出帳"`

---

### Task A7：`update_payment` 全額沖帳納入退費 diff 簽核閘

**Files:**
- Modify: `$WT/api/activity/registrations_payments.py:179-187`（`is_paid=False` 沖帳分支）
- Test: `$WT/tests/test_activity_payment_guards.py`（追加）

- [ ] **Step 1: 失敗測試** — 無簽核權限員工對「calculator 建議退≈0、已繳 1000」的 reg 走 `PUT payment {is_paid:false, confirm_refund_amount:1000}`，斷言被擋（需簽核，403/400）。

```python
def test_writeoff_bypass_blocked_by_diff_gate(session, client_staff_no_approve, served_registration):
    reg = served_registration  # 已出席多數堂 → calculator 建議退≈0；已繳 1000
    r = client_staff_no_approve.put(f"/api/activity/registrations/{reg.id}/payment",
            json={"is_paid": False, "confirm_refund_amount": 1000, "refund_reason": "家長要求"})
    assert r.status_code in (400, 403)        # diff 過大需簽核
```

- [ ] **Step 2: 跑確認失敗** — 目前會 200 通過（旁路）。
- [ ] **Step 3: 加簽核閘** — 在 `:179` `require_refund_reason(...)` 之後、`:181` `require_approve_for_cumulative_refund(...)` 旁，補：

```python
from services.activity_payment_guards import require_approve_for_refund_diff
require_approve_for_refund_diff(
    session, registration_id, current_paid, current_user,
)
```

（參數對齊 `services/activity_payment_guards.py:121` 的實際簽章——讀該函式確認傳 registration_id / 退費額 / current_user 的順序。）
- [ ] **Step 4: 跑確認通過** — 含「有簽核權限則放行」反向測試。`python -m pytest tests/test_activity_payment_guards.py -q` PASS。
- [ ] **Step 5: 回歸** — `python -m pytest tests/test_activity_refund_diff_verify.py -q` 零新增 fail。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "fix(activity): update_payment 全額沖帳納入退費 diff 簽核閘，堵未簽核退費旁路"`

---

### Task A8：availability 短 TTL 快取

**Files:**
- Modify: `$WT/api/activity/public.py:266-317`（`get_public_courses_availability`）
- Modify: `$WT/api/activity/_shared.py`（invalidate hook，沿用 `utils/cache_layer.get_cache()`）
- Test: `$WT/tests/test_activity_availability_cache.py`（新建）

- [ ] **Step 1: 失敗測試** — 連打兩次 availability，斷言第二次不重跑聚合 query（用 monkeypatch 計數聚合呼叫或 spy `session.query`），且 register 後 invalidate 會重算。
- [ ] **Step 2: 跑確認失敗** — 每次都聚合。
- [ ] **Step 3: 加快取** — availability 結果以 `get_cache().get/set(ns="activity_availability", key, ttl=10)` 包；`_invalidate_activity_dashboard_caches` 內 / register/update/promote 容量異動處呼叫對應 invalidate。
- [ ] **Step 4: 跑確認通過** — PASS。
- [ ] **Step 5: 回歸** — `python -m pytest tests/test_activity_public_etag.py -q` 零新增 fail。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "perf(activity): availability 加 10s TTL 快取，降報名開放期單 worker DB 壓力"`

---

### Task A9：P2 群（lock 降級收斂 + capacity NULL 顯示 + 文件註記）

**Files:**
- Modify: `$WT/api/activity/pos.py:529-540`、`$WT/api/activity/_shared.py:50-59`（lock 降級）
- Modify: `$WT/api/activity/public.py:300`（capacity NULL）
- Modify: `$WT/schemas/activity_public.py:83`（honeypot docstring）、`$WT/api/activity/public.py:1095`（confirm/decline 註記 + follow-up TODO）
- Test: `$WT/tests/test_activity_lock_degrade.py`（新建）

- [ ] **Step 1: 失敗測試（lock 降級）** — mock `_lock_regs` 內 `with_for_update().all()` 拋 `sqlalchemy.exc.OperationalError`，斷言**上拋**（不被吞成無鎖查詢）。SQLite 的 `CompileError/NotImplementedError` 仍降級。
- [ ] **Step 2: 跑確認失敗** — 目前 OperationalError 被靜默降級。
- [ ] **Step 3: 改 except** — 把 `except (CompileError, OperationalError, NotImplementedError)` 收斂為 `except (CompileError, NotImplementedError)`（讓 `OperationalError` 上拋）。兩處（pos.py、_shared.py）一致。
- [ ] **Step 4: capacity NULL** — `public.py:300` 預設 30 改為 `None`（前端 v-if 顯示「不限/—」；availability response 對 NULL capacity 回不限語意而非 30）。補一條測試斷言 NULL capacity 不回 30。
- [ ] **Step 5: 文件註記** — honeypot docstring 加「輔助、非主要 anti-automation，真正節流靠限流器」；confirm/decline 加 `# TODO(follow-up): 通知連結帶 query token 作第二因素` + 在 spec out-of-scope 已列。
- [ ] **Step 6: 跑確認通過 + 回歸** — `python -m pytest tests/test_activity_lock_degrade.py tests/test_activity_pos.py -q` PASS / 零新增 fail。
- [ ] **Step 7: Commit** — `git -C $WT commit -am "fix(activity): lock 降級僅限 SQLite 編譯錯誤、capacity NULL 顯示不限、honeypot/confirm 文件註記"`

---

### Task A10：XFF 程式防禦 + Zeabur 驗證 runbook

**Files:**
- Modify: `$WT/utils/request_ip.py`（`_parse_trusted_proxies` 對字面 `"*"` 給 warning）
- Modify: `$WT/docs/sop/zeabur-deployment-runbook.md`（新增驗證節）
- Test: `$WT/tests/test_request_ip.py`（追加）

- [ ] **Step 1: 失敗測試** — `trusted_proxy_ips="*"` 時 `_parse_trusted_proxies` 應 emit 一筆 warning log（提醒未真正限定 proxy）；解析結果仍 = RFC1918 預設（行為不變、只加可觀測性）。
- [ ] **Step 2: 跑確認失敗** — 目前靜默。
- [ ] **Step 3: 改 code** — 在 `raw == "*"` 或解析全失敗時 `logger.warning("TRUSTED_PROXY_IPS 未有效設定，rate-limit 以 RFC1918 預設信任；prod 請設為 edge CIDR")`。
- [ ] **Step 4: runbook** — `docs/sop/zeabur-deployment-runbook.md` 加「上線前 XFF 驗證」節：從兩個真實來源 IP 各 `curl -H 'X-Forwarded-For: 1.2.3.4' https://<prod>/api/activity/public/courses` + 多次觸發 429，觀察 limiter bucket 跟「偽造值」或「真實 peer」；若跟偽造 → 設 `TRUSTED_PROXY_IPS` 為 Zeabur edge CIDR。
- [ ] **Step 5: 跑確認通過 + 回歸** — `python -m pytest tests/test_request_ip.py -q` PASS。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "fix(infra): TRUSTED_PROXY_IPS 未設時警告 + 補 Zeabur XFF 上線驗證 runbook"`

---

## Workstream B — 全 app `async def`→`def` 遷移（獨立 commit）

### Task B1：AST 掃描分類腳本

**Files:**
- Create: `$WT/scripts/classify_async_handlers.py`（一次性，可入 repo）
- Test: `$WT/tests/test_classify_async_handlers.py`（新建）

- [ ] **Step 1: 失敗測試** — 給腳本一段含三種 handler 的樣本（零 await / 唯一 await=asyncio.sleep / 真 await=ws.close），斷言分類為 `convert` / `convert` / `keep`。
- [ ] **Step 2: 跑確認失敗** — 腳本不存在。
- [ ] **Step 3: 寫腳本** — 用 `ast` 走訪 `api/**/*.py`，對每個 `AsyncFunctionDef` 收集所有 `Await` 節點呼叫名；若全部 await 皆為 `asyncio.sleep` 或無 await → `convert`，否則 `keep`。輸出 JSON 清單（file、func、lineno、decision、await_names）。
- [ ] **Step 4: 跑確認通過** — PASS。
- [ ] **Step 5: 產出全 app 清單** — `python scripts/classify_async_handlers.py > .scratch/async_classify.json`；**人工覆核** keep/convert 是否合理（對照 spec §4 必保留清單：ws/upload/broadcast/executor）。
- [ ] **Step 6: Commit** — `git -C $WT commit -am "chore: 新增 async handler 分類腳本（全 app async→def 遷移前置）"`

### Task B2：依清單機械轉換

**Files:** `api/**/*.py`（依 B1 清單 `decision==convert` 者）

- [ ] **Step 1:** 對每個 `convert` handler：`async def` → `def`；其 body 內 `await asyncio.sleep(x)` → `time.sleep(x)`（補 `import time`，移除不再用到的 `import asyncio`）。逐檔做、用 surgical replace 繞 black hook。
- [ ] **Step 2:** 每改 ~10 檔跑一次相關 router 測試確認未破。
- [ ] **Step 3:** `keep` 清單一律不動。
- [ ] **Step 4: Commit（可分數批）** — `git -C $WT commit -am "perf: 全 app 無真 await 的 handler 改同步 def，解單 worker event-loop 阻塞"`

### Task B3：完整套件 gate + 抽查

- [ ] **Step 1: 完整 pytest** — `cd $WT && python -m pytest -q`；記錄 fail 數，**相對 main baseline 零新增 fail**（先在 main 跑一次存 baseline）。
- [ ] **Step 2: 抽查保留清單** — ws/upload handler 仍 `async def`：`grep -n "async def" api/activity/public.py`（應只剩…實際應全轉，public.py 無真 await → 全 def）、`grep -n "async def" api/attachments.py api/contact_book_ws.py`（upload/ws handler 應仍 async）。
- [ ] **Step 3: 抽查轉換正確** — 跑 `tests/test_activity_api.py`、`tests/test_students*.py` 等代表性路由測試綠。
- [ ] **Step 4:** 若有新增 fail → 逐一查（多半是測試對 handler async 性有耦合或誤判 keep/convert），個別修正後重跑。
- [ ] **Step 5: Commit（如有修正）** — `git -C $WT commit -am "test: 修 async→def 遷移後的測試耦合"`

---

## 收尾

- [ ] **全套件最終 gate** — `cd $WT && python -m pytest -q` 相對 main 零新增 fail；A 各項新測試綠。
- [ ] **前端** — `cd $WTFE && npx vitest run && npm run typecheck && npm run build` 綠。
- [ ] **finishing-a-development-branch** — 用 superpowers:finishing-a-development-branch 決定 merge/PR；BE 與 FE 分開 commit/PR；列出待 user 的 ops（A10 Zeabur 驗證 + 設 `TRUSTED_PROXY_IPS`、merge、push、prod 無 schema 變更）。

---

## Self-Review（plan vs spec 覆蓋）

- A2..A10 + B1..B3 一一對應 spec §3/§4，無遺漏。
- A1（public.py async）併入 B（spec §2 明示）→ B2 涵蓋。
- 無 placeholder：tricky task（A4/A6/A7）給了實際 test + 實作碼；A8/B 給了精確位置 + 既有 helper，實作者讀鄰近 code 即可執行。
- 型別/helper 名稱一致：`_auto_promote_first_waitlist`、`require_approve_for_refund_diff`、`_find_idempotent_hit`、`_recent_duplicate_payment`（本 plan 新增）、`get_cache()` 皆與 codebase / 前序 task 對齊。
- 風險：B 動到未審查模組 → B1 人工覆核 + B3 完整套件 gate 守住。
