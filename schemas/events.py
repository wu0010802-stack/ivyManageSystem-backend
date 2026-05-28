"""School Events (api/events.py) Out schemas — Phase 3.5。

對應 router 端：
- ``_event_to_dict``                  → ``EventOut``
- ``_event_to_dict`` + ``message``    → ``EventMutationResultOut``（create/update）
- ``_school_event_to_feed_dict``      → ``EventCalendarFeedItemOut``（superset，
  涵蓋 ``_official_item_to_feed_dict``，因 official item 也用 dict 填同 keys）
- ``build_admin_calendar_feed`` 回傳  → ``EventCalendarFeedOut``
- ``get_cached_official_sync_status`` → ``EventCalendarOfficialSyncOut``
- ``import_holidays`` results dict    → ``HolidayImportResultOut``

`calendar_admin.py` 的 ``CalendarFeedResponse`` 是另一個 endpoint（家長/教師
admin_feed 用 ``from_``/``to``/``items`` envelope），與本檔 ``EventCalendarFeedOut``
的 ``{year, month, events, official_sync}`` shape 不同，名稱刻意不衝突。

`get_holiday_import_template` 因回 ``StreamingResponse(xlsx)`` 非 JSON 形態，
保留 grandfather（不在此檔覆蓋）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class EventOut(IvyBaseModel):
    """單筆行事曆事件（對應 router 端 ``_event_to_dict``）。

    日期欄位皆為 router 已 ``.isoformat()`` 後的 str；recurrence_rule 為 JSON dict。
    """

    id: int
    title: str
    description: Optional[str] = None
    event_date: str
    end_date: Optional[str] = None
    event_type: str
    event_type_label: str
    is_all_day: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    recurrence_rule: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EventMutationResultOut(EventOut):
    """create_event / update_event 回傳 — 同 EventOut 並多 ``message`` 提示。

    繼承 EventOut 保留全部 event 欄位（前端目前不依賴但保留兼容性，避免
    從 ``{message, id}`` 強縮減造成既有 e2e/log 噪音）。
    """

    message: str


class EventCalendarOfficialSyncOut(IvyBaseModel):
    """``build_admin_calendar_feed`` 中 ``official_sync`` 區塊（與
    ``get_cached_official_sync_status`` shape 一致）。"""

    status: str
    warning: Optional[str] = None
    used_cache: bool
    last_synced_at: Optional[str] = None


class EventCalendarFeedItemOut(IvyBaseModel):
    """``build_admin_calendar_feed`` events[] 單筆（superset）。

    包含人工事件（``_school_event_to_feed_dict``）與官方假日/補班日
    （``_official_item_to_feed_dict``）兩種 producer 共用的欄位集合；官方
    item 的 ``id`` 為 ``"{kind}-{int}"`` 字串，人工事件為 int → 用 ``int | str``。
    """

    id: int | str
    title: str
    description: Optional[str] = None
    event_date: str
    end_date: Optional[str] = None
    event_type: str
    event_type_label: str
    is_all_day: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    is_official: bool
    is_read_only: bool
    official_kind: Optional[str] = None


class EventCalendarFeedOut(IvyBaseModel):
    """``GET /events/calendar-feed`` 回傳（= ``build_admin_calendar_feed`` 結果）。"""

    year: int
    month: int
    events: list[EventCalendarFeedItemOut]
    official_sync: EventCalendarOfficialSyncOut


class HolidayImportResultOut(IvyBaseModel):
    """``POST /events/holidays/import`` 回傳。

    對齊 router 端 ``results`` dict shape：total / upserted / failed / errors /
    stale_marked（成功才會有；失敗或全部都 raise 中斷時 commit 路徑未跑到）。
    """

    total: int
    upserted: int
    failed: int
    errors: list[str]
    # commit 前才寫入；HTTPException path 不會回此 shape，故仍標 Optional 保險
    stale_marked: Optional[int] = None
