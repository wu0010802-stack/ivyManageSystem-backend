"""三個排程的 missed-window catch-up 觸發判斷（純函式）。

回歸：原本用精準日期/分鐘相等比對，當觸發時刻整段停機就永久錯過該次。
改為「到點/過點即補跑、且去重」的純函式，便於單元測試。

- graduation：畢業日當天～畢業日後 grace 天內補跑（有界，避免無界補跑誤畢業下一屆）
- recruitment_term_advance：start_date 落在 [today-grace, today] 的 term（advance 對已 active idempotent）
- finance_reconciliation：到/過每日目標時刻且當日尚未跑
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


# ── graduation ─────────────────────────────────────────────────────────────
class TestGraduationShouldRun:
    def _target(self, year):
        from services.graduation_scheduler import graduation_date_for_year

        return graduation_date_for_year(year)

    def test_runs_on_target_day(self):
        from services.graduation_scheduler import should_run_auto_graduation

        t = self._target(2026)
        assert should_run_auto_graduation(t, last_run_year=None) is True

    def test_catch_up_within_grace_window(self):
        """畢業日當天停機 → 隔幾天開機仍補跑（grace 內）。"""
        from services.graduation_scheduler import (
            CATCHUP_GRACE_DAYS,
            should_run_auto_graduation,
        )
        from datetime import timedelta

        t = self._target(2026)
        assert (
            should_run_auto_graduation(
                t + timedelta(days=CATCHUP_GRACE_DAYS), last_run_year=None
            )
            is True
        )

    def test_not_run_before_target(self):
        from datetime import timedelta

        from services.graduation_scheduler import should_run_auto_graduation

        t = self._target(2026)
        assert (
            should_run_auto_graduation(t - timedelta(days=1), last_run_year=None)
            is False
        )

    def test_not_run_after_grace_window(self):
        """超過 grace 視窗不補跑（避免無界補跑誤畢業新學年學生）。"""
        from datetime import timedelta

        from services.graduation_scheduler import (
            CATCHUP_GRACE_DAYS,
            should_run_auto_graduation,
        )

        t = self._target(2026)
        assert (
            should_run_auto_graduation(
                t + timedelta(days=CATCHUP_GRACE_DAYS + 1), last_run_year=None
            )
            is False
        )

    def test_not_run_if_already_ran_this_year(self):
        from services.graduation_scheduler import should_run_auto_graduation

        t = self._target(2026)
        assert should_run_auto_graduation(t, last_run_year=2026) is False


# ── recruitment term advance ────────────────────────────────────────────────
class TestTermAdvanceWindow:
    def test_window_includes_today_and_recent_past(self):
        from services.recruitment_term_advance_scheduler import (
            TERM_ADVANCE_CATCHUP_DAYS,
            term_start_date_window,
        )
        from datetime import timedelta

        today = date(2026, 9, 1)
        lo, hi = term_start_date_window(today)
        assert hi == today
        assert lo == today - timedelta(days=TERM_ADVANCE_CATCHUP_DAYS)


# ── finance reconciliation ──────────────────────────────────────────────────
class TestReconciliationShouldRun:
    def test_runs_at_target_time(self):
        from services.finance_reconciliation_scheduler import should_run_reconciliation

        now = datetime(2026, 5, 29, 2, 0, tzinfo=TAIPEI)
        assert should_run_reconciliation(now, last_run_date=None) is True

    def test_catch_up_after_target_minute(self):
        """重啟落在 02:01 仍應於當天觸發（原本精準 02:00 會整天錯過）。"""
        from services.finance_reconciliation_scheduler import should_run_reconciliation

        now = datetime(2026, 5, 29, 2, 1, tzinfo=TAIPEI)
        assert should_run_reconciliation(now, last_run_date=None) is True

    def test_not_run_before_target_time(self):
        from services.finance_reconciliation_scheduler import should_run_reconciliation

        now = datetime(2026, 5, 29, 1, 59, tzinfo=TAIPEI)
        assert should_run_reconciliation(now, last_run_date=None) is False

    def test_not_run_twice_same_day(self):
        from services.finance_reconciliation_scheduler import should_run_reconciliation

        now = datetime(2026, 5, 29, 3, 0, tzinfo=TAIPEI)
        assert should_run_reconciliation(now, last_run_date=date(2026, 5, 29)) is False


# ── data quality ─────────────────────────────────────────────────────────────
class TestDataQualityShouldRun:
    """data_quality 排程的觸發判斷。target 時刻可設定（不像 finance 是常數）。

    回歸：原本 now.minute == target_minute 精準分鐘比對 + 60s 巡檢，相位漂移後
    輪詢落在 target 分鐘之外即整天錯過；改為 >= 目標時刻 + 當日去重。
    """

    def test_runs_at_target_time(self):
        from services.data_quality_scheduler import should_run_data_quality

        now = datetime(2026, 5, 29, 3, 0, tzinfo=TAIPEI)
        assert should_run_data_quality(now, 3, 0, last_run_date=None) is True

    def test_catch_up_after_target_minute(self):
        """輪詢相位漂移落在 03:05（非精準 03:00）仍應於當天觸發（原本整天錯過）。"""
        from services.data_quality_scheduler import should_run_data_quality

        now = datetime(2026, 5, 29, 3, 5, tzinfo=TAIPEI)
        assert should_run_data_quality(now, 3, 0, last_run_date=None) is True

    def test_not_run_before_target_time(self):
        from services.data_quality_scheduler import should_run_data_quality

        now = datetime(2026, 5, 29, 2, 59, tzinfo=TAIPEI)
        assert should_run_data_quality(now, 3, 0, last_run_date=None) is False

    def test_not_run_twice_same_day(self):
        from services.data_quality_scheduler import should_run_data_quality

        now = datetime(2026, 5, 29, 3, 5, tzinfo=TAIPEI)
        assert (
            should_run_data_quality(now, 3, 0, last_run_date=date(2026, 5, 29)) is False
        )
