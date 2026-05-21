"""Background scheduler enable/interval/time-of-day settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Activity waitlist
    activity_waitlist_sweeper_enabled: BoolEnv = False
    activity_waitlist_scheduler_enabled: BoolEnv = False
    activity_waitlist_sweep_interval_seconds: int = 600
    activity_waitlist_check_interval: int = 300
    activity_waitlist_reminder_offset_hours: int = 24
    activity_waitlist_final_reminder_offset_hours: int = 6
    activity_waitlist_confirm_window_hours: int = 48

    # Medication reminder (default 對齊 services/medication_reminder_scheduler.py 原碼)
    medication_reminder_enabled: BoolEnv = False
    medication_reminder_check_interval: int = 300
    medication_reminder_hour: int = 7
    medication_reminder_minute: int = 30

    # Auto graduation (default 對齊 services/graduation_scheduler.py 原碼)
    auto_graduation_enabled: BoolEnv = False
    auto_graduation_check_interval: int = 3600
    auto_graduation_month: int = 7
    auto_graduation_day: int = 31
    auto_graduation_preview_days: int = 7

    # Salary auto snapshot
    salary_auto_snapshot_enabled: BoolEnv = False
    salary_snapshot_check_interval: int = 86400

    # Official calendar sync
    official_calendar_sync_enabled: BoolEnv = False
    official_calendar_sync_interval: int = 86400

    # Misc schedulers
    finance_reconciliation_enabled: BoolEnv = False
    security_gc_disabled: BoolEnv = False
