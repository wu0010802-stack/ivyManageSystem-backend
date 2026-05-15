"""T6 範例：用 monkeypatch 凍結 _today_taipei() 測 graduation preview 視窗。

Review T6 指出：整 backend 0 個 freezegun → 年初/年底跑時間敏感測試結果
不同。本檔示範用 stdlib `monkeypatch.setattr` 達到同樣的時間隔離效果，
不引入新依賴。

凍結時間的兩種寫法：
1. 函式接受 today: Optional[date]=None → 測試直接傳 today（最乾淨）
2. 函式內部呼叫 module-level _today_taipei() → monkeypatch.setattr(mod, ...)

本檔示範第 2 種，因為它能驗證「不傳參數時的 default 行為」也跑對。
與既有 test_salary_snapshot.py:505 的 monkeypatch _today_taipei 同模式。
"""

from datetime import date

import pytest

from services import graduation_scheduler

# ------------------------------------------------------------------
# 預告視窗（PREVIEW_WINDOW_DAYS 預設 7）
# ------------------------------------------------------------------


class TestPreviewWindow:
    @pytest.mark.parametrize(
        "frozen_today,expected_in_window",
        [
            # 畢業日 7/31 當天
            (date(2026, 7, 31), True),
            # 畢業日前 1 天 → 在視窗內
            (date(2026, 7, 30), True),
            # 畢業日前 7 天（PREVIEW_WINDOW_DAYS）→ 邊界仍在內
            (date(2026, 7, 24), True),
            # 畢業日前 8 天 → 不在視窗
            (date(2026, 7, 23), False),
            # 年初 → 不在視窗
            (date(2026, 1, 15), False),
            # 畢業後 1 天 → 不在視窗（is_within 不含畢業後）
            (date(2026, 8, 1), False),
        ],
    )
    def test_is_within_preview_window_under_frozen_date(
        self, monkeypatch, frozen_today, expected_in_window
    ):
        """凍結 _today_taipei() 為各種日期，驗證 default 路徑（不傳 today）
        判定正確。"""
        monkeypatch.setattr(graduation_scheduler, "_today_taipei", lambda: frozen_today)
        assert graduation_scheduler.is_within_preview_window() == expected_in_window

    def test_upcoming_graduation_date_after_this_years_target(self, monkeypatch):
        """8/1 之後算明年畢業日（this_year < today → roll forward）。
        Regression: 不凍結時間時 CI 跑在 8/1 之後測試行為不同。"""
        monkeypatch.setattr(
            graduation_scheduler, "_today_taipei", lambda: date(2026, 8, 5)
        )
        assert graduation_scheduler.upcoming_graduation_date() == date(2027, 7, 31)

    def test_upcoming_graduation_date_before_this_years_target(self, monkeypatch):
        """7/30 → 仍為今年畢業日。"""
        monkeypatch.setattr(
            graduation_scheduler, "_today_taipei", lambda: date(2026, 7, 30)
        )
        assert graduation_scheduler.upcoming_graduation_date() == date(2026, 7, 31)

    def test_explicit_today_overrides_frozen_default(self, monkeypatch):
        """API 接受 today 參數時，明確傳入應 override default。"""
        monkeypatch.setattr(
            graduation_scheduler, "_today_taipei", lambda: date(2026, 1, 1)
        )
        # 明確傳 7/30 → 應該回視窗內，不受 default 影響
        assert (
            graduation_scheduler.is_within_preview_window(today=date(2026, 7, 30))
            is True
        )
