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

from api.overtimes import (
    _to_time,
    _times_overlap,
    _check_overtime_overlap,
    _revoke_comp_leave_grant,
    _assert_within_monthly_cap,
    _validate_overtime_type_matches_calendar,
)
from services.overtime_conflict_service import (
    _assert_within_quarterly_cap,
    _shift_month,
)
from fastapi import HTTPException
from utils.constants import MAX_QUARTERLY_OVERTIME_HOURS

# ── 測試用 helpers ────────────────────────────────────────────────────────────


def _mock_session(records):
    """回傳指定記錄的假 session（支援 .query().filter().all()/.first() 鏈式呼叫）。

    注意：mock 的 filter() 為 no-op，不實際執行 SQL 條件。
    傳入 records 應代表「SQL 過濾後會回傳的結果集」。
    """

    class _Q:
        def filter(self, *a, **kw):
            return self

        def all(self):
            return records

        def first(self):
            return records[0] if records else None

    class _S:
        def query(self, *a):
            return _Q()

    return _S()


def _make_record(start, end, status="pending"):
    """建立 OvertimeRecord mock（start/end 可為 str / time / datetime）。"""
    r = types.SimpleNamespace()
    r.start_time = start
    r.end_time = end
    r.status = status
    return r


# ──────────────────────────────────────────────
# _to_time 型別正規化
# ──────────────────────────────────────────────
class TestToTime:

    def test_parses_hhmm_string(self):
        """'18:00' → time(18, 0)"""
        assert _to_time("18:00") == time(18, 0)

    def test_parses_string_with_minutes(self):
        """'09:30' → time(9, 30)"""
        assert _to_time("09:30") == time(9, 30)

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
        assert _times_overlap("17:00", "20:00", "18:00", "21:00") is True

    def test_non_overlapping_before(self):
        """16:00-17:00 在 18:00-20:00 之前，不重疊"""
        assert _times_overlap("16:00", "17:00", "18:00", "20:00") is False

    def test_non_overlapping_after(self):
        """20:00-21:00 在 17:00-19:00 之後，不重疊"""
        assert _times_overlap("20:00", "21:00", "17:00", "19:00") is False

    def test_adjacent_endpoints_not_overlapping(self):
        """17:00-18:00 與 18:00-19:00 相接（開放端點），不算重疊"""
        assert _times_overlap("17:00", "18:00", "18:00", "19:00") is False

    def test_string_vs_time_object_no_type_error(self):
        """
        Bug 復現：字串 '18:00' 與 datetime.time 物件混型輸入。
        修復前：直接比較 '18:00' < time(20, 0) → TypeError
        修復後：_times_overlap 統一轉為 time 物件後比較，不拋例外。
        """
        assert _times_overlap("18:00", "21:00", time(17, 0), time(20, 0)) is True

    def test_string_vs_datetime_object_no_type_error(self):
        """字串 '18:00' 與 datetime 物件混型輸入，不應 TypeError"""
        dt_start = datetime(2026, 1, 15, 17, 0)
        dt_end = datetime(2026, 1, 15, 20, 0)
        assert _times_overlap("18:00", "21:00", dt_start, dt_end) is True

    def test_datetime_vs_time_object_no_type_error(self):
        """datetime 物件與 datetime.time 物件混型輸入，不應 TypeError"""
        dt_start = datetime(2026, 1, 15, 18, 0)
        dt_end = datetime(2026, 1, 15, 21, 0)
        assert _times_overlap(dt_start, dt_end, time(17, 0), time(20, 0)) is True

    def test_contained_range(self):
        """17:00-21:00 完全包含 18:00-19:00，重疊"""
        assert _times_overlap("17:00", "21:00", "18:00", "19:00") is True


# ──────────────────────────────────────────────
# _check_overtime_overlap 行為測試（mock session）
# ──────────────────────────────────────────────
class TestCheckOvertimeOverlap:

    def test_no_existing_records_returns_none(self):
        """無任何既有記錄 → 不重疊，回傳 None"""
        session = _mock_session([])
        result = _check_overtime_overlap(
            session,
            1,
            date(2026, 1, 15),
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
            session,
            1,
            date(2026, 1, 15),
            datetime(2026, 1, 15, 17, 0),
            datetime(2026, 1, 15, 20, 0),
        )
        assert result is existing

    def test_overlapping_record_returned(self):
        """時間重疊時回傳衝突記錄（SQL 過濾後 DB 回傳該筆）。

        18:00-21:00 與 17:00-20:00 重疊，mock 模擬 DB 過濾後回傳該筆。
        """
        existing = _make_record(time(17, 0), time(20, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session,
            1,
            date(2026, 1, 15),
            "18:00",
            "21:00",
        )
        assert result is existing

    def test_non_overlapping_returns_none(self):
        """時間不重疊 → DB 端過濾後無結果，回傳 None。

        重疊過濾邏輯已移至 SQL 層（start_time < end AND end_time > start）。
        mock 傳入空 records，模擬 DB 將不重疊記錄過濾掉的結果。
        """
        session = _mock_session([])
        result = _check_overtime_overlap(
            session,
            1,
            date(2026, 1, 15),
            "17:00",
            "20:00",
        )
        assert result is None

    def test_missing_time_info_treats_as_overlap(self):
        """缺少時間資訊（None）→ 同日即視為重疊，回傳既有記錄"""
        existing = _make_record(time(17, 0), time(20, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session,
            1,
            date(2026, 1, 15),
            None,
            None,
        )
        assert result is existing

    def test_adjacent_times_not_overlap(self):
        """相接不重疊：新申請 18:00-20:00，既有 16:00-18:00 → 不重疊。

        SQL 過濾條件 start_time < end_time AND end_time > start_time 使用嚴格不等式，
        相接端點不算重疊，DB 不回傳該記錄。mock 傳入空 records 模擬此結果。
        """
        session = _mock_session([])
        result = _check_overtime_overlap(
            session,
            1,
            date(2026, 1, 15),
            "18:00",
            "20:00",
        )
        assert result is None


# ──────────────────────────────────────────────
# _revoke_comp_leave_grant 補休撤銷邏輯
# ──────────────────────────────────────────────


def _make_ot(ot_id, employee_id, ot_date, hours, use_comp=True, comp_granted=True):
    ot = types.SimpleNamespace()
    ot.id = ot_id
    ot.employee_id = employee_id
    ot.overtime_date = ot_date
    ot.hours = hours
    ot.use_comp_leave = use_comp
    ot.comp_leave_granted = comp_granted
    return ot


def _make_leave(leave_id, hours, status, source_overtime_id=None):
    """建立 LeaveRecord mock，status 為 'approved'/'rejected'/'pending'。
    For callers passing True/False/None (legacy), those are converted positionally.
    """
    # Accept bool/None for backwards-compat with existing call sites
    if status is True:
        status = "approved"
    elif status is False:
        status = "rejected"
    elif status is None:
        status = "pending"
    lv = types.SimpleNamespace()
    lv.id = leave_id
    lv.leave_hours = hours
    lv.status = status
    lv.source_overtime_id = source_overtime_id
    lv.rejection_reason = None
    return lv


def _make_quota(total_hours):
    q = types.SimpleNamespace()
    q.total_hours = total_hours
    return q


class _MockQuery:
    """支援 .filter().all() 以及 .filter().scalar() 的 session mock，
    可依 query target 分別回傳不同結果。"""

    def __init__(self, linked_leaves, quota, approved_h=0.0, pending_h=0.0):
        self._linked_leaves = linked_leaves
        self._quota = quota
        self._approved_h = approved_h
        self._pending_h = pending_h
        self._target = None
        self._filters = []

    def query(self, target):
        self._target = target
        self._filters = []
        return self

    def filter(self, *args):
        self._filters.extend(args)
        return self

    def all(self):
        return self._linked_leaves

    def first(self):
        return self._quota

    def scalar(self):
        # 判斷是 approved 還是 pending 查詢
        # P2: filters now use status column; compile with literal_binds to see value
        from sqlalchemy.dialects import sqlite as _sqlite

        def _compiled(f):
            try:
                return str(
                    f.compile(
                        dialect=_sqlite.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )
            except Exception:
                return str(f)

        filter_strs = [_compiled(f) for f in self._filters]
        if any(
            "is_approved IS NULL" in s or "IS NULL" in s or "'pending'" in s
            for s in filter_strs
        ):
            return self._pending_h
        return self._approved_h


class TestRevokeCompLeaveGrant:

    def _make_session(self, linked_leaves, quota, approved_h=0.0, pending_h=0.0):
        return _MockQuery(linked_leaves, quota, approved_h, pending_h)

    def test_no_op_when_comp_leave_not_granted(self):
        """comp_leave_granted=False → 直接返回，不做任何變更"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0, comp_granted=False)
        session = self._make_session([], _make_quota(0.0))
        _revoke_comp_leave_grant(session, ot)
        assert ot.comp_leave_granted is False  # 保持原狀

    def test_no_op_when_use_comp_leave_false(self):
        """use_comp_leave=False → 直接返回"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0, use_comp=False, comp_granted=True)
        session = self._make_session([], _make_quota(8.0))
        _revoke_comp_leave_grant(session, ot)
        assert ot.comp_leave_granted is True  # 未被修改

    def test_quota_revoked_when_no_linked_leaves_and_no_committed(self):
        """無關聯假單、無已提交補休 → 配額成功撤銷"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0)
        quota = _make_quota(8.0)
        session = self._make_session([], quota, approved_h=0.0, pending_h=0.0)
        _revoke_comp_leave_grant(session, ot)
        assert quota.total_hours == 0.0
        assert ot.comp_leave_granted is False

    def test_raises_409_when_linked_approved_leave_exists(self):
        """有關聯的已核准補休假單 → 拋出 409，不撤銷配額"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0)
        quota = _make_quota(8.0)
        linked = [_make_leave(101, 8.0, True, source_overtime_id=1)]
        session = self._make_session(linked, quota, approved_h=8.0, pending_h=0.0)
        with pytest.raises(HTTPException) as exc_info:
            _revoke_comp_leave_grant(session, ot)
        assert exc_info.value.status_code == 409
        assert "101" in exc_info.value.detail
        assert quota.total_hours == 8.0  # 配額未被修改

    def test_auto_rejects_pending_linked_leaves_and_revokes_quota(self):
        """有關聯的待審核補休假單 → 自動駁回，配額成功撤銷"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0)
        quota = _make_quota(8.0)
        pending_lv = _make_leave(201, 8.0, None, source_overtime_id=1)
        # 模擬 autoflush 後 pending_h=0（因為 linked 已被駁回）
        session = self._make_session([pending_lv], quota, approved_h=0.0, pending_h=0.0)
        from models.approval import ApprovalStatus

        _revoke_comp_leave_grant(session, ot)
        assert pending_lv.status == ApprovalStatus.REJECTED.value
        assert pending_lv.rejection_reason is not None
        assert quota.total_hours == 0.0
        assert ot.comp_leave_granted is False

    def test_raises_409_when_unlinked_committed_exceeds_new_total(self):
        """無關聯假單但全域 committed 超過撤銷後配額 → 拋出 409（舊資料兼容）"""
        ot = _make_ot(1, 10, date(2026, 3, 1), 8.0)
        quota = _make_quota(8.0)
        session = self._make_session([], quota, approved_h=0.0, pending_h=6.0)
        with pytest.raises(HTTPException) as exc_info:
            _revoke_comp_leave_grant(session, ot)
        assert exc_info.value.status_code == 409
        assert quota.total_hours == 8.0  # 配額未被修改


# ──────────────────────────────────────────────
# Bug 回歸：OvertimeCreate / OvertimeUpdate 起迄時間順序驗證
# ──────────────────────────────────────────────
class TestOvertimeTimeOrderValidation:
    """
    Bug 描述：OvertimeCreate / OvertimeUpdate 只驗證個別時間格式，
    未跨欄位驗證 start_time < end_time。
    可以提交 start_time="20:00", end_time="08:00"（顛倒），導致重疊檢查失效。
    """

    def test_create_reversed_times_raises(self):
        """start_time > end_time → 422 ValidationError"""
        import pytest
        from pydantic import ValidationError
        from api.overtimes import OvertimeCreate
        from datetime import date as _date

        with pytest.raises(ValidationError) as exc_info:
            OvertimeCreate(
                employee_id=1,
                overtime_date=_date(2026, 3, 20),
                overtime_type="weekday",
                start_time="20:00",
                end_time="08:00",
                hours=2.0,
            )
        assert "start_time" in str(exc_info.value) or "end_time" in str(exc_info.value)

    def test_create_equal_times_raises(self):
        """start_time == end_time → 422 ValidationError"""
        import pytest
        from pydantic import ValidationError
        from api.overtimes import OvertimeCreate
        from datetime import date as _date

        with pytest.raises(ValidationError):
            OvertimeCreate(
                employee_id=1,
                overtime_date=_date(2026, 3, 20),
                overtime_type="weekday",
                start_time="10:00",
                end_time="10:00",
                hours=2.0,
            )

    def test_create_correct_order_passes(self):
        """start_time < end_time → 建立成功，不拋例外"""
        from api.overtimes import OvertimeCreate
        from datetime import date as _date

        obj = OvertimeCreate(
            employee_id=1,
            overtime_date=_date(2026, 3, 20),
            overtime_type="weekday",
            start_time="18:00",
            end_time="20:00",
            hours=2.0,
        )
        assert obj.start_time == "18:00"
        assert obj.end_time == "20:00"

    def test_create_no_times_passes(self):
        """start_time / end_time 皆為 None 時，不觸發時間順序驗證"""
        from api.overtimes import OvertimeCreate
        from datetime import date as _date

        obj = OvertimeCreate(
            employee_id=1,
            overtime_date=_date(2026, 3, 20),
            overtime_type="weekday",
            hours=2.0,
        )
        assert obj.start_time is None

    def test_update_reversed_times_raises(self):
        """OvertimeUpdate: start_time > end_time → 422 ValidationError"""
        import pytest
        from pydantic import ValidationError
        from api.overtimes import OvertimeUpdate

        with pytest.raises(ValidationError):
            OvertimeUpdate(start_time="22:00", end_time="09:00")

    def test_update_correct_order_passes(self):
        """OvertimeUpdate: start_time < end_time → 建立成功"""
        from api.overtimes import OvertimeUpdate

        obj = OvertimeUpdate(start_time="09:00", end_time="11:00")
        assert obj.start_time == "09:00"


# ──────────────────────────────────────────────
# 法定加班倍率下限（勞基法第 24 條）
# ──────────────────────────────────────────────
class TestStatutoryOvertimeRates:
    """倍率常數不得低於勞基法法定下限。

    勞基法第 24 條第 2 項：休息日工作前 2 小時「加給 1/3 以上」，
    即至少 1 + 1/3 ≈ 1.3333...，實務與勞動部範例四捨五入後以 1.34 為下限。
    """

    def test_restday_first_2h_rate_not_below_statutory_minimum(self):
        from utils.constants import RESTDAY_FIRST_2H_RATE

        assert (
            RESTDAY_FIRST_2H_RATE >= 1.34
        ), f"休息日前 2 小時倍率 {RESTDAY_FIRST_2H_RATE} 低於法定下限 1.34"

    def test_restday_mid_rate_not_below_statutory_minimum(self):
        from utils.constants import RESTDAY_MID_RATE

        assert RESTDAY_MID_RATE >= 1.67

    def test_weekday_rates_not_below_statutory_minimum(self):
        from utils.constants import WEEKDAY_FIRST_2H_RATE, WEEKDAY_AFTER_2H_RATE

        assert WEEKDAY_FIRST_2H_RATE >= 1.34
        assert WEEKDAY_AFTER_2H_RATE >= 1.67


# ──────────────────────────────────────────────
# 每月延長工時上限（勞基法第 32 條第 2 項）
# ──────────────────────────────────────────────
class TestMonthlyOvertimeCap:
    """每月延長工時不得超過 46 小時（勞基法第 32 條第 2 項）。

    _assert_within_monthly_cap 為純函式：接收既有累計時數 + 新增時數，
    超過法定上限時拋 HTTPException 400。
    """

    def test_exactly_at_cap_passes(self):
        """既有 40h + 新 6h = 46h（等於上限），允許"""
        _assert_within_monthly_cap(40.0, 6.0, 2026, 3)

    def test_zero_existing_full_cap_passes(self):
        """單筆填滿 46h 上限，允許"""
        _assert_within_monthly_cap(0.0, 46.0, 2026, 3)

    def test_just_over_cap_raises(self):
        """既有 40h + 新 7h = 47h，超過 46h 上限 → 400"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_monthly_cap(40.0, 7.0, 2026, 3)
        assert exc.value.status_code == 400
        assert "46" in exc.value.detail

    def test_zero_existing_over_cap_raises(self):
        """單筆 46.5h 直接超過上限 → 400"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_monthly_cap(0.0, 46.5, 2026, 3)
        assert exc.value.status_code == 400

    def test_none_hours_treated_as_zero(self):
        """None 視為 0，不應拋例外"""
        _assert_within_monthly_cap(None, 10.0, 2026, 3)
        _assert_within_monthly_cap(10.0, None, 2026, 3)


# ──────────────────────────────────────────────
# 國定假日加班類型驗證（勞基法第 37 條）
# ──────────────────────────────────────────────
class TestOvertimeTypeCalendarValidation:
    """overtime_type 需與該日是否為國定假日一致，避免短付加班費。

    - overtime_type="holiday" 但該日非國定假日 → 400（防止溢付）
    - overtime_type="weekday"/"weekend" 但該日為國定假日 → 400（防止短付，違反第 37 條）
    """

    def test_holiday_type_on_actual_holiday_passes(self):
        _validate_overtime_type_matches_calendar("holiday", True)

    def test_holiday_type_on_non_holiday_raises(self):
        with pytest.raises(HTTPException) as exc:
            _validate_overtime_type_matches_calendar("holiday", False)
        assert exc.value.status_code == 400

    def test_weekday_type_on_holiday_raises(self):
        """國定假日誤標為平日 → 會短付（× 1.34 而非 × 2.0）"""
        with pytest.raises(HTTPException) as exc:
            _validate_overtime_type_matches_calendar("weekday", True)
        assert exc.value.status_code == 400
        assert "holiday" in exc.value.detail

    def test_weekend_type_on_holiday_raises(self):
        with pytest.raises(HTTPException) as exc:
            _validate_overtime_type_matches_calendar("weekend", True)
        assert exc.value.status_code == 400

    def test_weekday_type_on_non_holiday_passes(self):
        _validate_overtime_type_matches_calendar("weekday", False)

    def test_weekend_type_on_non_holiday_passes(self):
        _validate_overtime_type_matches_calendar("weekend", False)


# ────────────────────────────────────────────────────────────────────
# 季 138h cap 純函式測試（勞基法 §32 II）
# ────────────────────────────────────────────────────────────────────


class TestAssertWithinQuarterlyCap:
    """純函式：worst_existing + new ≤ 138.0 = pass，否則 raise 400 含 6 要素"""

    def test_boundary_138_exact_passes(self):
        """138.0 剛好不算超過（與 monthly cap 同口徑 + 1e-9 tolerance）"""
        _assert_within_quarterly_cap(132.0, 6.0, "2026/03~2026/05", 1)

    def test_over_138_blocks(self):
        """138.1 即 raise"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_quarterly_cap(132.0, 6.2, "2026/03~2026/05", 1)
        assert exc.value.status_code == 400
        assert "超過勞基法第 32 條" in exc.value.detail

    def test_none_safety(self):
        """None 輸入不會 crash"""
        _assert_within_quarterly_cap(None, 10.0, "2026/03~2026/05", 1)
        _assert_within_quarterly_cap(10.0, None, "2026/03~2026/05", 1)

    def test_message_contains_six_required_fields(self):
        """訊息必含：員工 ID、窗口、累計、新筆、合計、上限"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_quarterly_cap(135.0, 5.0, "2026/03~2026/05", 42)
        msg = exc.value.detail
        assert "#42" in msg
        assert "2026/03~2026/05" in msg
        assert "135.0" in msg
        assert "5.0" in msg
        assert "140.0" in msg
        assert "138" in msg
        assert "勞基法第 32 條第 2 項" in msg


class TestShiftMonth:
    """月份位移 helper：正/負 offset、跨年 wrap"""

    def test_positive_offset_within_year(self):
        assert _shift_month(2026, 5, 2) == (2026, 7)

    def test_positive_offset_cross_year(self):
        assert _shift_month(2026, 11, 3) == (2027, 2)

    def test_negative_offset_within_year(self):
        assert _shift_month(2026, 5, -2) == (2026, 3)

    def test_negative_offset_cross_year(self):
        assert _shift_month(2026, 2, -3) == (2025, 11)

    def test_zero_offset_noop(self):
        assert _shift_month(2026, 5, 0) == (2026, 5)

    def test_december_plus_one_wraps_to_january_next_year(self):
        """關鍵邊界：month=12, offset=1 時 total % 12 = 0，+1 後 = 1"""
        assert _shift_month(2026, 12, 1) == (2027, 1)


# ────────────────────────────────────────────────────────────────────
# admin endpoint-level 整合測試（季 138h cap，mock monthly off）
# ────────────────────────────────────────────────────────────────────
#
# 這兩條測試透過 TestClient 打真實 HTTP endpoint，驗證 quarterly cap
# 在 admin create / approve 路徑均已生效（defense-in-depth）。
#
# mock monthly cap off 是必要的：現行 46h/月嚴卡讓單筆很難只觸季 cap，
# mock 後可獨立驗 quarterly 的守衛行為。
#
# Portal 整合 mock-verifying test 見 tests/test_portal_overtimes_guards.py。
# ────────────────────────────────────────────────────────────────────

import io as _io
import os as _os
import sys as _sys

import models.base as _base_module
from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient as _TestClient
from openpyxl import Workbook as _Workbook
from sqlalchemy import create_engine as _create_engine
from sqlalchemy.orm import sessionmaker as _sessionmaker
from unittest.mock import MagicMock as _MagicMock, patch as _patch

from api.auth import router as _auth_router, _account_failures, _ip_attempts
from api.overtimes import router as _overtimes_router
from models.database import (
    Base as _Base,
    Employee as _Employee,
    OvertimeRecord as _OvertimeRecord,
    User as _User,
)
from utils.auth import hash_password as _hash_password
import api.overtimes as _overtimes_module


@pytest.fixture
def _admin_app_client(tmp_path, monkeypatch):
    """Isolated SQLite + mini FastAPI app for admin endpoint integration tests."""
    db_path = tmp_path / "admin-quarterly.sqlite"
    engine = _create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = _sessionmaker(bind=engine)

    old_engine = _base_module._engine
    old_session_factory = _base_module._SessionFactory
    _base_module._engine = engine
    _base_module._SessionFactory = session_factory

    _Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    fake_salary_engine = _MagicMock()
    monkeypatch.setattr(_overtimes_module, "_salary_engine", fake_salary_engine)
    # PR-D (2026-05-26): _line_service global removed; dispatch.enqueue 接管

    app = _FastAPI()
    app.include_router(_auth_router)
    app.include_router(_overtimes_router)

    # batch-create 沿用 _batch_approve_limiter（10/60s）；測試多次 POST 會累積觸發 429。
    # 以 FastAPI dependency override 在測試中停用此限流（限流本身由 rate_limit 單元測試覆蓋）。
    app.dependency_overrides[_overtimes_module._batch_approve_limiter] = lambda: None

    with _TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _base_module._engine = old_engine
    _base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_emp(
    session, employee_id: str = "AQCI001", name: str = "季測員工"
) -> "_Employee":
    e = _Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _make_admin_user(session, username: str = "hr_admin") -> "_User":
    """純管理員（employee_id=None），不觸發自我核准守衛。"""
    u = _User(
        employee_id=None,
        username=username,
        password_hash=_hash_password("AdminPass123"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _seed_ot(
    session, emp_id: int, ot_date: date, hours: float, status="approved"
) -> "_OvertimeRecord":
    ot = _OvertimeRecord(
        employee_id=emp_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=hours,
        overtime_pay=0.0,
        status=status,
    )
    session.add(ot)
    session.flush()
    return ot


def _do_login(client: "_TestClient") -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "hr_admin", "password": "AdminPass123"},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


class TestAdminOvertimeQuarterlyCapBoundary:
    """admin /api/overtimes endpoint-level：mock monthly off 後驗 quarterly raise。"""

    def test_admin_create_blocks_when_quarterly_cap_exceeded(self, _admin_app_client):
        """seed W1 (3~5月) = 135h approved，admin POST 3h on 5/20 → W1=138 boundary pass。
        然後 POST 5h on 5/21 → W1=143 → 400 含 "138" 與窗口 label。

        mock monthly off 讓第二筆（月累計會超 46h）可以只觸 quarterly cap。
        """
        client, session_factory = _admin_app_client
        with session_factory() as session:
            emp = _make_emp(session)
            _make_admin_user(session)
            emp_id = emp.id
            # W1 (2026/03~2026/05) seed = 45+45+45 = 135h
            _seed_ot(session, emp_id, date(2026, 3, 5), 45.0)
            _seed_ot(session, emp_id, date(2026, 4, 5), 45.0)
            _seed_ot(session, emp_id, date(2026, 5, 5), 45.0)
            session.commit()

        _do_login(client)

        # 第一筆 3h → W1=138，剛好不超，應 pass（boundary check）
        with _patch(
            "api.overtimes._check_monthly_overtime_cap",
            return_value=None,
        ):
            resp1 = client.post(
                "/api/overtimes",
                json={
                    "employee_id": emp_id,
                    "overtime_date": "2026-05-20",
                    "overtime_type": "weekday",
                    "hours": 3.0,
                    "start_time": "18:00",
                    "end_time": "21:00",
                },
            )
        assert (
            resp1.status_code == 201
        ), f"boundary 3h（W1=138）應 pass，但 got {resp1.status_code}: {resp1.text}"

        # 第二筆 5h → W1 現在 = 138+5 = 143，超過 → 400 含 "138" + 窗口
        with _patch(
            "api.overtimes._check_monthly_overtime_cap",
            return_value=None,
        ):
            resp2 = client.post(
                "/api/overtimes",
                json={
                    "employee_id": emp_id,
                    "overtime_date": "2026-05-21",
                    "overtime_type": "weekday",
                    "hours": 5.0,
                    "start_time": "18:00",
                    "end_time": "23:00",
                },
            )
        assert (
            resp2.status_code == 400
        ), f"W1=143 應被 quarterly cap 擋下（400），但 got {resp2.status_code}: {resp2.text}"
        detail = resp2.json().get("detail", "")
        assert "138" in detail, f"detail 應含 '138'，got: {detail!r}"
        assert (
            "2026/03~2026/05" in detail
        ), f"detail 應含 '2026/03~2026/05'，got: {detail!r}"

    def test_admin_approve_pending_blocks_when_quarterly_cap_exceeded(
        self, _admin_app_client
    ):
        """seed approved 130h + 1 pending 10h，approve 時 W1=140 > 138 → 400 含 "138"。

        approve endpoint 在 `approved and not was_approved` 路徑重新跑 quarterly cap 檢查，
        確保核准時仍守門（舊資料/direct DB 改寫漏洞防護）。

        mock monthly off：5/5 已 40h，再 +10h = 50h > 46h，月上限會先擋；
        mock 後讓 quarterly cap 獨立生效。
        """
        client, session_factory = _admin_app_client
        with session_factory() as session:
            emp = _make_emp(session, employee_id="AQCI002", name="季測員工B")
            _make_admin_user(session)
            emp_id = emp.id
            # W1 (2026/03~2026/05) approved = 45+45+40 = 130h
            _seed_ot(session, emp_id, date(2026, 3, 5), 45.0)
            _seed_ot(session, emp_id, date(2026, 4, 5), 45.0)
            _seed_ot(session, emp_id, date(2026, 5, 5), 40.0)
            # 1 pending 10h → 若 approve，W1 = 130+10 = 140 > 138
            pending = _seed_ot(
                session, emp_id, date(2026, 5, 20), 10.0, status="pending"
            )
            pending_id = pending.id
            session.commit()

        _do_login(client)

        # PUT /api/overtimes/{id}/approve，approved=True → quarterly cap 應擋
        with _patch(
            "api.overtimes._check_monthly_overtime_cap",
            return_value=None,
        ):
            resp = client.put(
                f"/api/overtimes/{pending_id}/approve",
                json={"approved": True},
            )

        assert resp.status_code == 400, (
            f"approve 時 W1=140 應被 quarterly cap 擋下（400），"
            f"but got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail", "")
        assert "138" in detail, f"detail 應含 '138'，got: {detail!r}"

        # 驗證 pending 仍是 pending，approve 失敗應整個 rollback，不應被誤 partial commit
        with session_factory() as session:
            refreshed = session.query(_OvertimeRecord).filter_by(id=pending_id).first()
            assert refreshed is not None, "pending record 應存在"
            assert (
                refreshed.status == "pending"
            ), f"approve 失敗應 rollback，pending 仍應為 status='pending'，但現在={refreshed.status}"


# ── _parse_hhmm_on_date / _validate_overtime_for_employee 共用 helper ──
from api.overtimes import _parse_hhmm_on_date, _validate_overtime_for_employee


class TestParseHhmmOnDate:
    def test_none_returns_none(self):
        assert _parse_hhmm_on_date(date(2026, 6, 5), None) is None

    def test_parses_to_datetime_on_given_date(self):
        dt = _parse_hhmm_on_date(date(2026, 6, 5), "14:30")
        assert dt == datetime(2026, 6, 5, 14, 30)


class TestValidateOvertimeForEmployee:
    """helper 必須沿用單筆建立的驗證鏈；overlap 命中時 raise 409。"""

    def test_raises_409_on_overlap(self):
        import types as _types

        existing = _types.SimpleNamespace(
            start_time=None,
            end_time=None,
            status="pending",
            id=42,
            overtime_date=date(2026, 6, 5),
        )
        session = _mock_session([existing])
        with pytest.raises(HTTPException) as exc:
            _validate_overtime_for_employee(
                session,
                employee_id=1,
                overtime_date=date(2026, 6, 5),
                overtime_type="weekday",
                start_dt=None,
                end_dt=None,
                hours=2.0,
            )
        assert exc.value.status_code == 409
        assert "時間重疊" in exc.value.detail


class TestBatchCreateOvertime:
    """POST /api/overtimes/batch-create：全部或全無 + 蒐集所有失敗。"""

    def _payload(self, emp_ids, hours=2.0, **kw):
        base = {
            "overtime_date": "2026-06-05",
            "overtime_type": "weekday",
            "reason": "校慶活動",
            "use_comp_leave": False,
            "employees": [{"employee_id": i, "hours": hours} for i in emp_ids],
        }
        base.update(kw)
        return base

    def test_all_pass_creates_all_pending(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            ids = [e1.id, e2.id]
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["created_ids"]) == 2
        with session_factory() as session:
            rows = session.query(_OvertimeRecord).all()
            assert len(rows) == 2
            assert all(r.status == "pending" for r in rows)

    def test_one_over_monthly_cap_aborts_whole_batch(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            e1_id, e2_id = e1.id, e2.id
            ids = [e1_id, e2_id]
            _seed_ot(session, e2_id, date(2026, 6, 1), 45.0)
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]["errors"]
        assert any(e["employee_id"] == e2_id for e in errors)
        with session_factory() as session:
            assert (
                session.query(_OvertimeRecord)
                .filter(_OvertimeRecord.employee_id == e1_id)
                .count()
                == 0
            )

    def test_collects_all_failures(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            e1_id, e2_id = e1.id, e2.id
            ids = [e1_id, e2_id]
            _seed_ot(session, e1_id, date(2026, 6, 1), 45.0)
            _seed_ot(session, e2_id, date(2026, 6, 1), 45.0)
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]["errors"]
        err_ids = {e["employee_id"] for e in errors}
        assert e1_id in err_ids and e2_id in err_ids

    def test_duplicate_employee_id_reported(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            _make_admin_user(session)
            eid = e1.id
            session.commit()
        _do_login(client)

        resp = client.post(
            "/api/overtimes/batch-create", json=self._payload([eid, eid])
        )
        assert resp.status_code == 422, resp.text
        with session_factory() as session:
            assert session.query(_OvertimeRecord).count() == 0

    def test_comp_leave_zero_pay_no_grant(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            _make_admin_user(session)
            eid = e1.id
            session.commit()
        _do_login(client)

        resp = client.post(
            "/api/overtimes/batch-create",
            json=self._payload([eid], use_comp_leave=True),
        )
        assert resp.status_code == 200, resp.text
        with session_factory() as session:
            row = (
                session.query(_OvertimeRecord)
                .filter(_OvertimeRecord.employee_id == eid)
                .first()
            )
            assert row.overtime_pay == 0.0
            assert row.use_comp_leave is True
            assert row.comp_leave_granted is False

    def test_per_employee_hours(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            e1_id, e2_id = e1.id, e2.id
            ids = [e1_id, e2_id]
            session.commit()
        _do_login(client)

        payload = self._payload(ids)
        payload["employees"][0]["hours"] = 2.0
        payload["employees"][1]["hours"] = 3.0
        resp = client.post("/api/overtimes/batch-create", json=payload)
        assert resp.status_code == 200, resp.text
        with session_factory() as session:
            by_emp = {
                r.employee_id: r.hours for r in session.query(_OvertimeRecord).all()
            }
            assert by_emp[e1_id] == 2.0
            assert by_emp[e2_id] == 3.0

    def test_requires_permission(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            u = _User(
                employee_id=None,
                username="noperm",
                password_hash=_hash_password("AdminPass123"),
                role="staff",
                permission_names=[],
                is_active=True,
                must_change_password=False,
            )
            session.add(u)
            eid = e1.id
            session.commit()
        resp = client.post(
            "/api/auth/login", json={"username": "noperm", "password": "AdminPass123"}
        )
        assert resp.status_code == 200
        resp = client.post("/api/overtimes/batch-create", json=self._payload([eid]))
        assert resp.status_code == 403
