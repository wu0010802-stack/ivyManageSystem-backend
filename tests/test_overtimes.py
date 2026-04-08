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

from api.overtimes import _to_time, _times_overlap, _check_overtime_overlap, _revoke_comp_leave_grant
from fastapi import HTTPException


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

    def test_overlapping_record_returned(self):
        """時間重疊時回傳衝突記錄（SQL 過濾後 DB 回傳該筆）。

        18:00-21:00 與 17:00-20:00 重疊，mock 模擬 DB 過濾後回傳該筆。
        """
        existing = _make_record(time(17, 0), time(20, 0))
        session = _mock_session([existing])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            '18:00', '21:00',
        )
        assert result is existing

    def test_non_overlapping_returns_none(self):
        """時間不重疊 → DB 端過濾後無結果，回傳 None。

        重疊過濾邏輯已移至 SQL 層（start_time < end AND end_time > start）。
        mock 傳入空 records，模擬 DB 將不重疊記錄過濾掉的結果。
        """
        session = _mock_session([])
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
        """相接不重疊：新申請 18:00-20:00，既有 16:00-18:00 → 不重疊。

        SQL 過濾條件 start_time < end_time AND end_time > start_time 使用嚴格不等式，
        相接端點不算重疊，DB 不回傳該記錄。mock 傳入空 records 模擬此結果。
        """
        session = _mock_session([])
        result = _check_overtime_overlap(
            session, 1, date(2026, 1, 15),
            '18:00', '20:00',
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


def _make_leave(leave_id, hours, is_approved, source_overtime_id=None):
    lv = types.SimpleNamespace()
    lv.id = leave_id
    lv.leave_hours = hours
    lv.is_approved = is_approved
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
        filter_strs = [str(f) for f in self._filters]
        if any("is_approved IS NULL" in s or "IS NULL" in s for s in filter_strs):
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
        _revoke_comp_leave_grant(session, ot)
        assert pending_lv.is_approved is False
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
