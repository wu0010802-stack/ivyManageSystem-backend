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
    "PII_RETENTION_GC_DISABLED",
    "PII_RETENTION_GC_DRY_RUN",
    "PII_RETENTION_TERMINAL_DAYS",
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
    # PII retention defaults: disabled + dry-run + 365 天
    assert s.pii_retention_gc_disabled is True
    assert s.pii_retention_gc_dry_run is True
    assert s.pii_retention_terminal_days == 365
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


def test_pii_retention_defaults(monkeypatch):
    """PII retention 預設安全：disabled + dry-run + 365 天。"""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    s = SchedulerSettings()
    assert s.pii_retention_gc_disabled is True
    assert s.pii_retention_gc_dry_run is True
    assert s.pii_retention_terminal_days == 365


def test_pii_retention_env_override(monkeypatch):
    """PII retention env override."""
    monkeypatch.setenv("PII_RETENTION_GC_DISABLED", "0")
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    monkeypatch.setenv("PII_RETENTION_TERMINAL_DAYS", "180")
    s = SchedulerSettings()
    assert s.pii_retention_gc_disabled is False
    assert s.pii_retention_gc_dry_run is False
    assert s.pii_retention_terminal_days == 180
