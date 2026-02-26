"""加班重疊檢查邏輯單元測試

Bug：_check_overtime_overlap 在 line 106 直接比較 start_time < record.end_time，
     未做型別正規化。當傳入字串 '18:00' 或 DB 回傳 datetime.time 物件時，
     Python 無法跨型別比較 → TypeError → HTTP 500，導致加班申請/修改全面崩潰。

修復：抽出 _to_time() 正規化 + _times_overlap() 純函式，
     統一轉為 datetime.time 後再比較。
"""
import types
import pytest
from datetime import date, time, datetime

from api.overtimes import _to_time, _times_overlap, _check_overtime_overlap


# ── 測試用 helpers ────────────────────────────────────────────────────────────

def _mock_session(records):
    """回傳指定記錄的假 session（支援 .query().filter().all() 鏈式呼叫）。"""
    class _Q:
        def filter(self, *a, **kw):
            return self

        def all(self):
            return records

    class _S:
        def query(self, *a):
            return _Q()

    return _S()


def _make_record(start, end, is_approved=None):
    """建立 OvertimeRecord mock（start/end 可為 str / time / datetime）。"""
    r = types.SimpleNamespace()
    r.start_time = start
    r.end_time = end
    r.is_approved = is_approved
    return r


# ──────────────────────────────────────────────
# _to_time 型別正規化
# ──────────────────────────────────────────────
class TestToTime:

    def test_parses_hhmm_string(self):
        """'18:00' → time(18, 0)"""
        assert _to_time('18:00') == time(18, 0)

    def test_parses_string_with_minutes(self):
        """'09:30' → time(9, 30)"""
        assert _to_time('09:30') == time(9, 30)

    def test_accepts_time_object(self):
        """time(18, 0) → time(18, 0)（原樣回傳）"""
        t = time(18, 0)
        assert _to_time(t) is t

    def test_extracts_time_from_datetime(self):
        """datetime(2026, 1, 15, 18, 0) → time(18, 0)"""
        dt = datetime(2026, 1, 15, 18, 0)
        assert _to_time(dt) == time(18, 0)


# ──────────────────────────────────────────────
# _times_overlap 區間重疊邏輯（純函式）
# ──────────────────────────────────────────────
class TestTimesOverlap:

    def test_overlapping_ranges(self):
        """17:00-20:00 與 18:00-21:00 重疊"""
        assert _times_overlap('17:00', '20:00', '18:00', '21:00') is True

    def test_non_overlapping_before(self):
        """16:00-17:00 在 18:00-20:00 之前，不重疊"""
        assert _times_overlap('16:00', '17:00', '18:00', '20:00') is False

    def test_non_overlapping_after(self):
        """20:00-21:00 在 17:00-19:00 之後，不重疊"""
        assert _times_overlap('20:00', '21:00', '17:00', '19:00') is False

    def test_adjacent_endpoints_not_overlapping(self):
        """17:00-18:00 與 18:00-19:00 相接（開放端點），不算重疊"""
        assert _times_overlap('17:00', '18:00', '18:00', '19:00') is False

    def test_string_vs_time_object_no_type_error(self):
        """
        Bug 復現：字串 '18:00' 與 datetime.time 物件混型輸入。
        修復前：直接比較 '18:00' < time(20, 0) → TypeError
        修復後：_times_overlap 統一轉為 time 物件後比較，不拋例外。
        """
        assert _times_overlap('18:00', '21:00', time(17, 0), time(20, 0)) is True

    def test_string_vs_datetime_object_no_type_error(self):
        """字串 '18:00' 與 datetime 物件混型輸入，不應 TypeError"""
        dt_start = datetime(2026, 1, 15, 17, 0)
        dt_end = datetime(2026, 1, 15, 20, 0)
        assert _times_overlap('18:00', '21:00', dt_start, dt_end) is True

    def test_datetime_vs_time_object_no_type_error(self):
        """datetime 物件與 datetime.time 物件混型輸入，不應 TypeError"""
        dt_start = datetime(2026, 1, 15, 18, 0)
        dt_end = datetime(2026, 1, 15, 21, 0)
        assert _times_overlap(dt_start, dt_end, time(17, 0), time(20, 0)) is True

    def test_contained_range(self):
        """17:00-21:00 完全包含 18:00-19:00，重疊"""
        assert _times_overlap('17:00', '21:00', '18:00', '19:00') is True


# ──────────────────────────────────────────────
# _check_overtime_overlap 行為測試（mock session）
# ──────────────────────────────────────────────
class TestCheckOvertimeOverlap:

    def test_no_existing_records_returns_none(self):
        """無任何既有記錄 → 不重疊，回傳 None"""
        session = _mock_session([])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            datetime(2026, 1, 15, 17, 0),
            datetime(2026, 1, 15, 20, 0),
        )
        assert result is None

    def test_datetime_vs_datetime_overlap_detected(self):
        """正常情境：datetime 與 datetime 重疊，回傳衝突記錄"""
        existing = _make_record(
            datetime(2026, 1, 15, 18, 0),
            datetime(2026, 1, 15, 21, 0),
        )
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            datetime(2026, 1, 15, 17, 0),
            datetime(2026, 1, 15, 20, 0),
        )
        assert result is existing

    def test_string_input_vs_time_obj_record_no_type_error(self):
        """
        Bug 復現：record.start_time / end_time 為 datetime.time 物件，
        傳入 start_time / end_time 為字串 '18:00' / '21:00'。
        修復前：'18:00' < time(20, 0) → TypeError → HTTP 500
        修復後：統一轉型後正確比對，回傳衝突記錄。
        """
        existing = _make_record(time(17, 0), time(20, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            '18:00', '21:00',
        )
        # 18:00-21:00 與 17:00-20:00 重疊，應回傳衝突記錄而非 TypeError
        assert result is existing

    def test_non_overlapping_returns_none(self):
        """時間不重疊 → 回傳 None"""
        existing = _make_record(time(14, 0), time(16, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            '17:00', '20:00',
        )
        assert result is None

    def test_missing_time_info_treats_as_overlap(self):
        """缺少時間資訊（None）→ 同日即視為重疊，回傳既有記錄"""
        existing = _make_record(time(17, 0), time(20, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            None, None,
        )
        assert result is existing

    def test_adjacent_times_not_overlap(self):
        """相接不重疊：新申請 18:00-20:00，既有 16:00-18:00 → 不重疊"""
        existing = _make_record(time(16, 0), time(18, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            '18:00', '20:00',
        )
        assert result is None
