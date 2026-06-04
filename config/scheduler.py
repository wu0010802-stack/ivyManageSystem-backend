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

    # Recruitment funnel term advance
    recruitment_term_advance_enabled: BoolEnv = True
    recruitment_term_advance_check_interval: int = 86400  # 1 天
    recruitment_term_advance_window_days: int = 90

    # Misc schedulers
    finance_reconciliation_enabled: BoolEnv = False
    security_gc_disabled: BoolEnv = False

    # P0b Audit log retention GC (spec 2026-05-28-audit-pii-redact-retention-design.md)
    # 預設 True：與 redaction-at-write 同步上線。HR 簽合規 SOP 前可在 prod 暫設 False。
    audit_gc_enabled: BoolEnv = True

    # Data quality (Ch2 of observability-forensic spec)
    data_quality_enabled: BoolEnv = False
    data_quality_check_interval: int = 60  # 每分鐘檢查是否到目標時間
    data_quality_hour: int = 3  # 03:00 Asia/Taipei
    data_quality_minute: int = 0

    # PDF worker (growth report background generation)
    # max_concurrency=4：避免單機群體生成壓垮 starlette threadpool（預設 40 slot）
    # recovery_enabled：啟動時把孤兒 'generating' row 標 failed。本 repo prod 走
    #   單 uvicorn worker，預設 True；若改 multi-worker 部署必須改 False，否則
    #   worker B 啟動會把 worker A 正在跑的 job 誤標 failed（leader 選舉 Phase 2）
    # job_timeout：單張 PDF 生成上限，超過視為 hang（log + 標 failed）
    pdf_worker_max_concurrency: int = 4
    pdf_worker_recovery_enabled: BoolEnv = True
    pdf_worker_job_timeout_seconds: int = 300
    pdf_worker_shutdown_timeout_seconds: int = 30

    # PII Retention GC（spec 2026-05-22-parent-pii-retention-data-export-design.md）
    # 預設全 OFF + dry-run：上線後 user 手動 review log 才開正式抹
    pii_retention_gc_disabled: BoolEnv = True
    pii_retention_gc_dry_run: BoolEnv = True
    pii_retention_terminal_days: int = 365
    # Employee 5y 退職 PII GC：抹 address / emergency_contact / bank_account（留身分證+薪資供稅務）
    employee_pii_retention_years: int = 5

    # Leave quota expiry（補休到期 + 特休週年 cutover）
    leave_quota_expiry_enabled: BoolEnv = False
    leave_quota_expiry_check_interval: int = 3600  # 1 小時輪詢一次

    # Announcement publish scheduler（spec 2026-05-29-announcement-improvements-design.md）
    announcement_publish_scheduler_enabled: BoolEnv = True
    announcement_publish_check_interval: int = 60  # 60 秒輪詢，足以讓「8:00 排程」最遲 8:01 推播

    # Academic term turnover（學期自動切換：唯一的學期切換驅動器）
    # 預設 True：關閉等於學期永不換（resolve_current 仍走日期推導，但結轉不觸發）。
    # 上線誤觸發已由 acadterm01 migration 靜默對齊防護。
    academic_term_turnover_enabled: BoolEnv = True
    academic_term_turnover_check_interval: int = 3600  # 1 小時輪詢
