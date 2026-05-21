# Scheduler Leader Election — PG advisory lock rollout

**日期**：2026-05-21
**Branch**：`feat/scheduler-leader-election-2026-05-21-backend`
**Worktree**：`ivy-backend/.claude/worktrees/scheduler-leader-election-2026-05-21-backend`

---

## 1. 背景與動機

`main.py` lifespan 啟動 9 個 in-process asyncio scheduler（waitlist sweeper / graduation / ivykids_sync / salary_snapshot / activity_waitlist / medication_reminder / security_gc / official_calendar / finance_reconciliation）。每個用獨立 env flag `*_ENABLED=1`，源碼註解多處寫「建議僅單一 worker 啟用」。

Zeabur 目前單 replica 撐得住，但若 autoscale 到 2+ replica → salary_snapshot 重複建 snapshot、graduation 重複發 LINE 通知、official_calendar 重複 fetch 上游。**已知且文檔化的限制，不是 bug**，但要為 scale 預備防線。

## 2. 設計選擇

**選用 PostgreSQL advisory lock**（非 APScheduler、非 arq）：
- 工具已存在：`utils/advisory_lock.try_scheduler_lock(session, scheduler_name, run_key)`
- 3 個 scheduler 已落地：graduation / medication_reminder / finance_reconciliation
- 0 新依賴；不動 9 個 loop 結構（while + sleep + stop_event 維持）
- `pg_try_advisory_xact_lock` 是 transaction-scoped，commit/rollback 自動釋放，worker 崩潰 connection 斷自動放
- 對比 APScheduler：v3 已停止維護、v4 是 rewrite，9 個異質 loop 重新建模成本高

**Trade-off**：advisory lock 方案保留「scheduler 與 web 同生命週期」的耦合。若未來 web tier scale up 但要 scheduler 不跟著重啟，仍需走 arq 拆 worker container — 但那是「水平擴展執行能力」目標，與 leader election 是不同的事，不要混。

## 3. 既有 helper API（沿用，不改）

```python
@contextmanager
def try_scheduler_lock(
    session: Session, *, scheduler_name: str, run_key: str
) -> Iterator[bool]:
    """非阻塞 advisory lock。
    yield True  → 已取得鎖，進入臨界區
    yield False → 已被其他 worker 持有，呼叫端應略過本次
    """
```

既有 pattern（從 finance_reconciliation 取）：
```python
session = get_session()  # 或 session_scope()
try:
    with try_scheduler_lock(session, scheduler_name="X", run_key="Y") as acquired:
        if not acquired:
            return {"...": "...", "skipped": True}
        # 實際工作
        session.commit()
```

## 4. Rollout 範圍

### 4.1 已落地（3 個）— 不動

| Scheduler | 文件 | scheduler_name | run_key |
|---|---|---|---|
| graduation | `services/graduation_scheduler.py:98` | `auto_graduation` | `effective_date.isoformat()` |
| medication_reminder | `services/medication_reminder_scheduler.py:81` | `medication_reminder` | `today.isoformat()` |
| finance_reconciliation | `services/finance_reconciliation_scheduler.py:83` | `finance_reconciliation` | `today.isoformat()` |

### 4.2 不適用（1 個）— 已有自家 lock

| Scheduler | 既有機制 |
|---|---|
| recruitment_ivykids_sync | `RecruitmentSyncState.sync_in_progress` 表級 row-lock + stale 自動釋放（`services/recruitment_ivykids_sync.py:715-755`）。功能等價，**不重複加 advisory lock** |

### 4.3 本次 rollout（5 個 scheduler、6 個 wrap 點）

| # | Scheduler | wrap 入口 | scheduler_name | run_key 設計 |
|---|---|---|---|---|
| W1 | official_calendar | `sync_official_calendar_once()` (services/official_calendar_scheduler.py:50) | `official_calendar` | `today.isoformat()`（日級工作） |
| W2 | salary_snapshot | `check_and_snapshot_once()` (services/salary_snapshot_scheduler.py:50) | `salary_snapshot` | `f"{year}-{month:02d}"`（目標月，已由內部 `_previous_month` 算出） |
| W3 | activity_waitlist | `check_and_sweep_once()` (services/activity_waitlist_scheduler.py:38) | `activity_waitlist_sweep` | `str(int(time.time() // 300))`（5 分鐘窗口 bucket） |
| W4 | security_gc / rate_limit | `_run_rate_limit_gc()` (services/security_gc_scheduler.py:62) | `security_rate_limit_gc` | `str(int(time.time() // 300))`（5 分鐘窗口 bucket） |
| W5 | security_gc / jwt_blocklist | `_run_jwt_blocklist_gc()` (services/security_gc_scheduler.py:72) | `security_jwt_blocklist_gc` | `str(int(time.time() // 21600))`（6 小時窗口 bucket） |
| W6 | main.py 內嵌 sweeper | `_activity_waitlist_sweeper()` (main.py:216) | `activity_waitlist_sweep`（**同 W3 namespace**） | 同 W3，5 分鐘 bucket |

**W6 注意**：`_activity_waitlist_sweeper` 與 W3 的 `activity_waitlist_scheduler` 完全重複（都 call `activity_service.sweep_expired_pending_promotions`），W3 是「仿 salary_snapshot 抽出的標準 scheduler」即繼任者。`_activity_waitlist_sweeper` 在 spec 標記 **deprecated**（不在這次刪除，避免擴大 scope）。共用 namespace 確保即便 user 兩個 flag 都開，互斥仍有效。

### 4.4 run_key 設計原則

- **「這件工作」的識別，不是「這次 tick」的時間戳**
- Daily job → `date.isoformat()`（e.g. `2026-05-21`）
- Monthly job → `f"{year}-{month:02d}"`（e.g. `2026-04`）
- Sweep/GC（無固定目標）→ interval bucket：`str(int(time.time() // interval_seconds))`，多 worker 在同 window 內只有一個會拿到 lock，下個 window 各自再競爭一次（不會永遠是同個 worker）

## 5. 測試策略

### 5.1 既有測試 pattern

graduation 與 finance_reconciliation 都有「兩次同 run_key call 第二次 skipped」的測試（PG fixture）。沿用。

### 5.2 本次新增

每個 wrap 點補一條測試：
- W1：`test_official_calendar_scheduler.py::test_lock_busy_skips`
- W2：`test_salary_snapshot_scheduler.py::test_lock_busy_skips`（先看是否已有 test 檔，避免重複）
- W3：`test_activity_waitlist_scheduler.py::test_lock_busy_skips`（檔已存在）
- W4 + W5：`test_security_gc_scheduler.py::{test_rate_limit_gc_lock_busy_skips, test_jwt_blocklist_gc_lock_busy_skips}`（新檔）
- W6：跳過獨立測試（與 W3 共用 lock namespace，W3 測試已覆蓋）

**SQLite 降級**：`try_scheduler_lock` 在 SQLite 直接 yield True（單寫入者語意，測試環境）。所以「兩次同 run_key 第二次 skipped」測試要：
1. 用 **PG fixture**（既有 conftest 已有 `postgres_session` 之類的 fixture，找一下確認）；或
2. **Mock**：unit test patch `try_scheduler_lock` 強制 yield False，斷言 caller 走 skipped 分支

優先 (2) — 避免 PG fixture 依賴擴散，pure unit test 確認 caller skip 行為。

### 5.3 整合驗證

`pytest -x -q` 全套通過、零 regression。pre-existing fail（`test_audit_router` / `test_supabase_storage`）允許 — 與本任務無關，confirm count 不變。

## 6. 不做的事

1. 不引入 APScheduler / arq / dramatiq / 新依賴
2. 不重構 9 個 loop 結構（while / sleep / stop_event 維持）
3. 不刪除 `_activity_waitlist_sweeper`（雖確認重複，標 deprecated 即可；避免 scope 擴大）
4. 不改 `utils/advisory_lock.try_scheduler_lock` API（已夠用）
5. 不改 `recruitment_ivykids_sync`（已有自家 lock，重複加會被 review 質疑）
6. 不動 `utils/rate_limit.py`（rate_limit 與 scheduler leader-election 是兩件事；rate_limit 已有 PG-backed 模式）
7. 不做 alembic migration（advisory lock 是 PG built-in，不需 schema 變更）

## 7. Commit 拆分

按 CLAUDE.md「一個 commit 只做一件事」：

1. `docs: 加 scheduler-leader-election spec`（本 spec）
2. `feat(scheduler): official_calendar 加 advisory lock leader election`（W1）
3. `feat(scheduler): salary_snapshot 加 advisory lock leader election`（W2）
4. `feat(scheduler): activity_waitlist_scheduler 加 advisory lock leader election`（W3）
5. `feat(scheduler): security_gc 兩個 GC 各加 advisory lock leader election`（W4 + W5，同檔合一個 commit）
6. `feat(scheduler): main.py 內嵌 sweeper 加 advisory lock（與 activity_waitlist_scheduler 共用 namespace；標 deprecated）`（W6）
7. `test(scheduler): 補 6 個 wrap 點的 lock skip 行為單元測試`
8. `docs: handoff doc + 完成總結`

預估 8 個 commit。

## 8. Merge 步驟（給 user）

worktree 完工後：
1. `cd ~/Desktop/ivy-backend && git fetch origin main`
2. `git checkout feat/scheduler-leader-election-2026-05-21-backend && git rebase origin/main`（若有衝突）
3. `git checkout main && git merge --ff-only feat/scheduler-leader-election-2026-05-21-backend`
4. `git push origin main`
5. 清 worktree：`git worktree remove .claude/worktrees/scheduler-leader-election-2026-05-21-backend`
6. 刪 branch：`git branch -d feat/scheduler-leader-election-2026-05-21-backend && git push origin --delete feat/scheduler-leader-election-2026-05-21-backend`

## 9. Follow-up（不在本次 scope）

- **刪 `_activity_waitlist_sweeper`**：確認 prod env 不再用 `ACTIVITY_WAITLIST_SWEEPER_ENABLED` flag 後可移除（main.py 縮 33 行）。
- **拆 worker container**：若需 scheduler 與 web tier 解耦，走 arq；新增工程。
- **CI smoke**：可加 hook 在 startup log 確認所有 enabled scheduler 都有 advisory lock guard（grep 性質的 lint）。
