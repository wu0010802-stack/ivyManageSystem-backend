import pytest
from config.scheduler import SchedulerSettings

_ALL_VARS = (
    "ACTIVITY_WAITLIST_SWEEPER_ENABLED",
    "ACTIVITY_WAITLIST_SCHEDULER_ENABLED",
    "ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS",
    "ACTIVITY_WAITLIST_CHECK_INTERVAL",
    "ACTIVITY_WAITLIST_REMINDER_OFFSET_HOURS",
    "ACTIVITY_WAITLIST_FINAL_REMINDER_OFFSET_HOURS",
    "ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS",
    "MEDICATION_REMINDER_ENABLED",
    "MEDICATION_REMINDER_CHECK_INTERVAL",
    "MEDICATION_REMINDER_HOUR",
    "MEDICATION_REMINDER_MINUTE",
    "AUTO_GRADUATION_ENABLED",
    "AUTO_GRADUATION_CHECK_INTERVAL",
    "AUTO_GRADUATION_MONTH",
    "AUTO_GRADUATION_DAY",
    "AUTO_GRADUATION_PREVIEW_DAYS",
    "SALARY_AUTO_SNAPSHOT_ENABLED",
    "SALARY_SNAPSHOT_CHECK_INTERVAL",
    "OFFICIAL_CALENDAR_SYNC_ENABLED",
    "OFFICIAL_CALENDAR_SYNC_INTERVAL",
    "FINANCE_RECONCILIATION_ENABLED",
    "SECURITY_GC_DISABLED",
)


def test_defaults(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    s = SchedulerSettings()
    # bool defaults: 全部 False（含 disabled flags 也是 False = 預設啟用 GC）
    assert s.activity_waitlist_sweeper_enabled is False
    assert s.medication_reminder_enabled is False
    assert s.auto_graduation_enabled is False
    assert s.salary_auto_snapshot_enabled is False
    assert s.official_calendar_sync_enabled is False
    assert s.finance_reconciliation_enabled is False
    assert s.security_gc_disabled is False
    # interval defaults
    assert s.activity_waitlist_sweep_interval_seconds == 600
    assert s.activity_waitlist_check_interval == 300
    assert s.medication_reminder_check_interval == 300
    assert s.auto_graduation_check_interval == 3600
    # time-of-day
    assert s.medication_reminder_hour == 7
    assert s.medication_reminder_minute == 30
    assert s.auto_graduation_month == 7
    assert s.auto_graduation_day == 31
    assert s.auto_graduation_preview_days == 7


def test_bool_env_parsing(monkeypatch):
    monkeypatch.setenv("MEDICATION_REMINDER_ENABLED", "yes")
    monkeypatch.setenv("ACTIVITY_WAITLIST_SWEEPER_ENABLED", "1")
    monkeypatch.setenv("SECURITY_GC_DISABLED", "TRUE")
    s = SchedulerSettings()
    assert s.medication_reminder_enabled is True
    assert s.activity_waitlist_sweeper_enabled is True
    assert s.security_gc_disabled is True


def test_int_parsing(monkeypatch):
    monkeypatch.setenv("ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS", "1200")
    monkeypatch.setenv("MEDICATION_REMINDER_HOUR", "9")
    s = SchedulerSettings()
    assert s.activity_waitlist_sweep_interval_seconds == 1200
    assert s.medication_reminder_hour == 9
