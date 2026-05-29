"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings, get_settings
from utils.sentry_init import capture_exception, init_sentry
from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine
from services.line_service import LineService
from services.line_login_service import LineLoginService

# Routers
from api.employees import router as employees_router, init_employee_services
from api.employees_docs import router as employees_docs_router
from api.students import router as students_router
from api.student_attendance import router as student_attendance_router
from api.student_incidents import router as student_incidents_router
from api.student_assessments import router as student_assessments_router
from api.classrooms import router as classrooms_router
from api.vendor_payments import router as vendor_payments_router
from api.monthly_fixed_costs import router as monthly_fixed_costs_router
from api.attendance import router as attendance_router
from api.salary import router as salary_router, init_salary_services
from api.disciplinary import router as disciplinary_router
from api.art_teacher_payroll import router as art_teacher_payroll_router
from api.system_config import router as system_config_router
from api.config import router as config_router, init_config_services
from api.leaves import (
    router as leaves_router,
    init_leaves_services,
)
from api.overtimes import (
    router as overtimes_router,
    init_overtimes_services,
)
from api.insurance import router as insurance_router, init_insurance_services
from api.auth import router as auth_router
from api.portal import router as portal_router, init_portal_notify_services
from api.shifts import router as shifts_router
from api.events import router as events_router
from api.meetings import router as meetings_router
from api.announcements import router as announcements_router
from api.approvals import router as approvals_router
from api.notifications import router as notifications_router
from api.reports import router as reports_router
from api.exports import router as exports_router
from api.audit import router as audit_router
from api.internal_metrics import router as internal_metrics_router
from api.integrations_health import router as integrations_health_router
from api.punch_corrections import router as punch_corrections_router
from api.approval_settings import router as approval_settings_router
from api.activity import router as activity_router
from api.dismissal_calls import (
    router as dismissal_calls_router,
    init_dismissal_line_service,
)
from api.dismissal_ws import ws_router as dismissal_ws_router
from api.contact_book_ws import ws_router as contact_book_ws_router
from api.line_webhook import router as line_webhook_router, init_webhook_service
from api.gov_reports import router as gov_reports_router, init_gov_report_services
from api.gov_moe import router as gov_moe_router
from api.fees import router as fees_router
from api.academic_terms import router as academic_terms_router
from api.recruitment import router as recruitment_router
from api.recruitment_ivykids import router as recruitment_ivykids_router
from api.recruitment_gov_kindergartens import (
    router as recruitment_gov_kindergartens_router,
)
from api.student_enrollment import router as student_enrollment_router
from api.student_change_logs import router as student_change_logs_router
from api.student_communications import router as student_communications_router
from api.bonus_preview import (
    router as bonus_preview_router,
    init_bonus_preview_services,
)
from api.health import router as health_router
from api.attachments import (
    router as attachments_router,
    download_router as attachments_download_router,
)
from api.portfolio import (
    auto_milestone_router,
    growth_reports_router,
    init_growth_reports_line_service,
    measurements_router,
    milestones_router,
    observations_router,
    student_attachments_router,
    timeline_router,
)
from api.student_health import router as student_health_router
from api.parent_portal import (
    parent_router as parent_portal_router,
    admin_router as parent_admin_router,
    init_parent_line_service,
)
from api.student_leaves import router as student_leaves_router
from api.appraisal import appraisal_router
from api.year_end import year_end_router
from api.calendar_admin import router as calendar_admin_router
from api.offboarding import router as offboarding_router
from services.leave_quota_expiry.comp_grant_reminder import (
    init_comp_grant_reminder_line_service,
)
from services.ops_alert import init_ops_alert_service

# Startup modules
from startup.migrations import run_alembic_upgrade
from startup.bootstrap import run_startup_bootstrap

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging():
    """設定日誌：生產環境使用 JSON 格式，開發環境使用可讀格式。

    所有 handler 一律掛 RequestIdLogFilter，讓任何 logger 出來的 record
    都帶 `request_id` 欄位（middleware 之外取到預設值 "-"）。
    """
    from utils.request_logging import RequestIdLogFilter

    level = logging.INFO
    rid_filter = RequestIdLogFilter()

    if settings.core.is_production:
        try:
            from pythonjsonlogger import jsonlogger

            handler = logging.StreamHandler()
            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
                    rename_fields={"asctime": "timestamp", "levelname": "level"},
                )
            )
            handler.addFilter(rid_filter)
            logging.root.handlers.clear()
            logging.root.addHandler(handler)
            logging.root.setLevel(level)
        except ImportError:
            # python-json-logger 未安裝時 fallback 到純文字
            logging.basicConfig(
                level=level,
                format="%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s]: %(message)s",
            )
            for h in logging.root.handlers:
                h.addFilter(rid_filter)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s]: %(message)s",
        )
        for h in logging.root.handlers:
            h.addFilter(rid_filter)


_configure_logging()
logger = logging.getLogger(__name__)

# Sentry 初始化（缺 SENTRY_DSN 時 no-op，安全在所有環境啟動）
# 必須在 FastAPI() 建構前 init，FastApiIntegration 才能 patch 進 router pipeline。
init_sentry()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

# Services (singletons)
insurance_service = InsuranceService()
# F3: 注入既有 InsuranceService singleton，避免 engine 自建第二份造成狀態分歧
salary_engine = SalaryEngine(load_from_db=True, insurance_service=insurance_service)
line_service = LineService()
# 家長入口 LIFF 認證；channel_id 可為空，端點會回 503 直到正確設定
line_login_service = LineLoginService(channel_id=settings.line.login_channel_id or "")


def on_startup():
    run_alembic_upgrade()
    run_startup_bootstrap(
        salary_engine, line_service, insurance_service=insurance_service
    )
    # bug sweep 2026-05-18 P2：啟動時預熱 CJK 字型註冊一次，讓任何 PDF 路由
    # 首次 hit 不會付 ~30ms TTFont parse 成本，且若 NotoSansTC-Regular.ttf 缺檔
    # 在啟動就 raise FileNotFoundError，比 production 跑到第一張 PDF 才爆好。
    try:
        from utils.pdf_fonts import register_cjk_font

        register_cjk_font()
    except Exception:
        logger.exception("CJK font 預熱失敗（PDF 端點將於首次呼叫時再試）")
    # 資安掃描 2026-05-07 P1：啟動時 log env，取代 /health/ready 公開暴露
    env_label = settings.core.env.lower()
    logger.info("Application started successfully (env=%s).", env_label)

    # term.changed handlers 註冊（import-time 觸發 @on_term_changed decorator）
    import services.term_subscribers.classroom_carry_over  # noqa: F401
    import services.term_subscribers.leave_quota_cutover  # noqa: F401
    import services.term_subscribers.activity_semester_tag  # noqa: F401

    from utils.term_events import list_handler_names

    logger.info("term.changed handlers: %s", list_handler_names())


async def _activity_waitlist_sweeper():
    """每 10 分鐘掃描候補轉正過期，發放逾期放棄與即將到期提醒。

    DEPRECATED：本 sweeper 與 ``services/activity_waitlist_scheduler.py`` 完全
    重複（兩者都呼叫 ``activity_service.sweep_expired_pending_promotions``）。
    後者是「仿 salary_snapshot 抽出的標準 scheduler」即繼任者，env flag
    ``ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1`` 啟用，預設 300 秒間隔。

    本函式仍保留以維護 backward compat（既有部署可能還用 ``ACTIVITY_WAITLIST_SWEEPER_ENABLED``
    舊 flag）。為避免 user 同時開兩個 flag 造成 LINE 通知重發，本 sweeper 用
    與 activity_waitlist_scheduler **相同** scheduler_name 共享 advisory lock
    namespace，互斥仍然有效。

    Follow-up（不在 leader-election rollout 範圍）：確認 prod 不再用舊 flag
    後可整段刪除（縮 main.py 約 33 行）。
    """
    import asyncio
    import time
    from services.activity_service import activity_service
    from models.database import get_session
    from utils.advisory_lock import try_scheduler_lock

    interval = settings.scheduler.activity_waitlist_sweep_interval_seconds
    logger.info("候補過期掃描器啟動，間隔 %s 秒", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            session = get_session()
            try:
                with try_scheduler_lock(
                    session,
                    scheduler_name="activity_waitlist_sweep",
                    run_key=str(int(time.time() // 300)),
                ) as acquired:
                    if not acquired:
                        session.commit()
                        continue
                    result = activity_service.sweep_expired_pending_promotions(session)
                    session.commit()
                    if result["expired"] or result["reminded"]:
                        logger.info(
                            "候補過期掃描：expired=%s reminded=%s",
                            result["expired"],
                            result["reminded"],
                        )
            finally:
                session.close()
        except asyncio.CancelledError:
            logger.info("候補過期掃描器收到取消訊號，退出")
            raise
        except Exception:
            logger.exception("候補過期掃描失敗（忽略本次）")


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    import asyncio

    # 註冊主 event loop，供 sync def 路由（thread pool）內呼叫 WS 廣播時使用。
    # contact_book_service.publish_entry 等位點透過 run_coroutine_threadsafe 投回主 loop，
    # 避免在背景 thread 起新 loop 造成 WS transport 跨 loop 失敗（誤踢訂閱者）。
    from utils.event_loop import set_main_loop

    _main_loop = asyncio.get_running_loop()
    app_instance.state.main_loop = _main_loop
    set_main_loop(_main_loop)

    # 通知中央 dispatcher：把 after_commit / after_rollback hook 綁到主庫 session factory
    from services.notification import dispatch as _notification_dispatch
    from models.base import get_session_factory as _get_factory

    _notification_dispatch.install_session_hooks(_get_factory())
    logger.info("notification dispatch hooks installed")

    on_startup()

    # PDF worker：啟動時若啟用 recovery，把上次 crash 留下的 'generating' 孤兒
    # 報告標 failed（避免 admin 看到永久 generating）。多 worker 部署只在 leader 開。
    if settings.scheduler.pdf_worker_recovery_enabled:
        try:
            from services.pdf_recovery import recover_orphan_pdf_jobs

            recover_orphan_pdf_jobs()
        except Exception as e:
            logger.warning("PDF orphan recovery 啟動失敗: %s", e)
            capture_exception(e, level="warning")

    # WS 廣播 backend（memory / redis 由 CACHE_BACKEND 切換）
    from utils.broadcast import get_broadcast as _get_broadcast

    _broadcast = _get_broadcast()
    await _broadcast.start()

    # 只有 env 啟用時才跑 sweeper（避免多 worker 重複發送通知）
    sweeper_task = None
    if settings.scheduler.activity_waitlist_sweeper_enabled:
        sweeper_task = asyncio.create_task(_activity_waitlist_sweeper())

    # 自動畢業排程：需要 AUTO_GRADUATION_ENABLED=1；建議僅在單一 worker 上啟用
    graduation_task = None
    graduation_stop_event: asyncio.Event | None = None
    try:
        from services import graduation_scheduler as _grad

        if _grad.scheduler_enabled():
            graduation_stop_event = asyncio.Event()
            graduation_task = asyncio.create_task(
                _grad.run_auto_graduation_scheduler(graduation_stop_event)
            )
    except Exception as e:
        logger.warning("自動畢業排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # Recruitment funnel term advance scheduler
    recruitment_term_advance_task = None
    recruitment_term_advance_stop_event: asyncio.Event | None = None
    try:
        from services import recruitment_term_advance_scheduler as _rt_sched

        if _rt_sched.scheduler_enabled():
            recruitment_term_advance_stop_event = asyncio.Event()
            recruitment_term_advance_task = asyncio.create_task(
                _rt_sched.run_recruitment_term_advance_scheduler(
                    recruitment_term_advance_stop_event
                )
            )
            logger.info("recruitment term advance scheduler 已啟用")
    except Exception as e:
        logger.warning("招生漏斗升學期排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # Leave quota expiry scheduler（補休到期 + 特休週年 cutover）
    leave_quota_expiry_task = None
    leave_quota_expiry_stop_event: asyncio.Event | None = None
    try:
        from services import leave_quota_expiry_scheduler as _lqe_sched

        if _lqe_sched.scheduler_enabled():
            leave_quota_expiry_stop_event = asyncio.Event()
            leave_quota_expiry_task = asyncio.create_task(
                _lqe_sched.run_leave_quota_expiry_scheduler(
                    leave_quota_expiry_stop_event
                )
            )
            logger.info("leave quota expiry scheduler 已啟用")
    except Exception as e:
        logger.warning("補休到期排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # LINE retry scheduler（Phase 2 P1 resilience）：每 5 min 重發 pending retry row
    line_retry_task = None
    line_retry_stop_event: asyncio.Event | None = None
    try:
        from services.notification.retry_scheduler import run_line_retry_scheduler

        line_retry_stop_event = asyncio.Event()
        line_retry_task = asyncio.create_task(
            run_line_retry_scheduler(line_retry_stop_event)
        )
        logger.info("LINE retry scheduler 已啟用")
    except Exception as e:
        logger.warning("LINE retry scheduler 啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # Phase 4 P1 resilience：pending_uploads scheduler（每 5 min 補傳 Supabase 失敗 file）
    pending_uploads_task = None
    pending_uploads_stop_event: asyncio.Event | None = None
    try:
        from services.notification.pending_uploads_scheduler import (
            run_pending_uploads_scheduler,
        )

        pending_uploads_stop_event = asyncio.Event()
        pending_uploads_task = asyncio.create_task(
            run_pending_uploads_scheduler(pending_uploads_stop_event)
        )
        logger.info("pending_uploads scheduler 已啟用")
    except Exception as e:
        logger.warning("pending_uploads scheduler 啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # Phase 4 P1 resilience：LINE token health scheduler（每日 08:00 ping /v2/bot/info）
    line_token_health_task = None
    line_token_health_stop_event: asyncio.Event | None = None
    try:
        from services.line_token_health_scheduler import run_line_token_health_scheduler

        line_token_health_stop_event = asyncio.Event()
        line_token_health_task = asyncio.create_task(
            run_line_token_health_scheduler(line_token_health_stop_event)
        )
        logger.info("LINE token health scheduler 已啟用")
    except Exception as e:
        logger.warning("LINE token health scheduler 啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 義華校官網自動同步：需要 IVYKIDS_SYNC_ENABLED=true + 帳密已設
    ivykids_sync_task = None
    ivykids_sync_stop_event: asyncio.Event | None = None
    try:
        from services import recruitment_ivykids_sync as _ivykids_sync

        if _ivykids_sync.scheduler_configured():
            ivykids_sync_stop_event = asyncio.Event()
            ivykids_sync_task = asyncio.create_task(
                _ivykids_sync.run_sync_scheduler(ivykids_sync_stop_event)
            )
    except Exception as e:
        logger.warning("義華校官網自動同步啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 薪資月底快照排程：需要 SALARY_AUTO_SNAPSHOT_ENABLED=1；建議僅在單一 worker 啟用
    salary_snapshot_task = None
    salary_snapshot_stop_event: asyncio.Event | None = None
    try:
        from services import salary_snapshot_scheduler as _snap_sched

        if _snap_sched.scheduler_enabled():
            salary_snapshot_stop_event = asyncio.Event()
            salary_snapshot_task = asyncio.create_task(
                _snap_sched.run_salary_snapshot_scheduler(salary_snapshot_stop_event)
            )
    except Exception as e:
        logger.warning("薪資月底快照排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 才藝候補名單過期掃描排程：需要 ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1；建議僅在單一 worker 啟用
    activity_waitlist_task = None
    activity_waitlist_stop_event: asyncio.Event | None = None
    try:
        from services import activity_waitlist_scheduler as _wl_sched

        if _wl_sched.scheduler_enabled():
            activity_waitlist_stop_event = asyncio.Event()
            activity_waitlist_task = asyncio.create_task(
                _wl_sched.run_activity_waitlist_scheduler(activity_waitlist_stop_event)
            )
            logger.info("activity waitlist scheduler 已啟用")
    except Exception as e:
        logger.warning("才藝候補名單排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 用藥提醒排程：需要 MEDICATION_REMINDER_ENABLED=1；建議僅在單一 worker 啟用
    medication_reminder_task = None
    medication_reminder_stop_event: asyncio.Event | None = None
    try:
        if settings.scheduler.medication_reminder_enabled:
            from services.medication_reminder_scheduler import (
                medication_reminder_loop,
            )

            medication_reminder_stop_event = asyncio.Event()
            medication_reminder_task = asyncio.create_task(
                medication_reminder_loop(medication_reminder_stop_event)
            )
    except Exception as e:
        logger.warning("用藥提醒排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 安全支援表 GC：rate_limit_buckets / jwt_blocklist；預設啟用，env 可關
    security_gc_task = None
    security_gc_stop_event: asyncio.Event | None = None
    try:
        from services import security_gc_scheduler as _sec_gc

        if _sec_gc.scheduler_enabled():
            security_gc_stop_event = asyncio.Event()
            security_gc_task = asyncio.create_task(
                _sec_gc.run_security_gc_scheduler(security_gc_stop_event)
            )
    except Exception as e:
        logger.warning("安全支援表 GC 排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # PII 保留期 GC：逾期帳號/訪客資料回收；預設 disabled + dry-run，user 手動 review log 後再啟用
    pii_retention_task = None
    pii_retention_stop_event: asyncio.Event | None = None
    try:
        from services import pii_retention_scheduler as _pii_gc

        if _pii_gc.scheduler_enabled():
            pii_retention_stop_event = asyncio.Event()
            pii_retention_task = asyncio.create_task(
                _pii_gc.run_pii_retention_scheduler(pii_retention_stop_event)
            )
    except Exception as e:
        logger.warning("PII 保留期 GC 排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 官方日曆每日同步：需要 OFFICIAL_CALENDAR_SYNC_ENABLED=1；建議僅單一 worker 啟用
    official_calendar_task = None
    official_calendar_stop_event: asyncio.Event | None = None
    try:
        from services import official_calendar_scheduler as _oc_sched

        if _oc_sched.scheduler_enabled():
            official_calendar_stop_event = asyncio.Event()
            official_calendar_task = asyncio.create_task(
                _oc_sched.run_official_calendar_scheduler(official_calendar_stop_event)
            )
    except Exception as e:
        logger.warning("官方日曆排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 才藝 POS paid_amount 對帳（spec H4）：需要 FINANCE_RECONCILIATION_ENABLED=1
    # 每日 02:00 Asia/Taipei 掃 active registrations，若 paid_amount 與
    # payment_records 淨額不一致即推 LINE 警示老闆
    finance_reconciliation_task = None
    finance_reconciliation_stop_event: asyncio.Event | None = None
    try:
        from services import finance_reconciliation_scheduler as _fr_sched

        if _fr_sched.scheduler_enabled():
            finance_reconciliation_stop_event = asyncio.Event()
            finance_reconciliation_task = asyncio.create_task(
                _fr_sched.run_finance_reconciliation_scheduler(
                    finance_reconciliation_stop_event
                )
            )
    except Exception as e:
        logger.warning("對帳排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    # 資料品質排程（spec 2026-05-28 observability-forensic Ch2.4）：
    # 每日 03:00 Asia/Taipei 跑 run_all_rules → dispatch.emit → flush_line_digest
    # 預設關閉（DATA_QUALITY_ENABLED=1 才啟用）；HR 確認 baseline 不雜音再開
    data_quality_task = None
    data_quality_stop_event: asyncio.Event | None = None
    try:
        from services import data_quality_scheduler as _dq_sched

        if _dq_sched.scheduler_enabled():
            data_quality_stop_event = asyncio.Event()
            data_quality_task = asyncio.create_task(
                _dq_sched.run_data_quality_scheduler(data_quality_stop_event)
            )
    except Exception as e:
        logger.warning("資料品質排程啟動失敗: %s", e)
        capture_exception(e, level="warning")

    try:
        yield
    finally:
        # 停 broadcast backend（先於 scheduler shutdown，避免 stop 期間還收到 publish）
        try:
            await _broadcast.stop()
        except Exception as exc:
            logger.warning("broadcast backend stop failed: %s", exc)
        if sweeper_task is not None:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
        if graduation_task is not None:
            if graduation_stop_event is not None:
                graduation_stop_event.set()
            try:
                await asyncio.wait_for(graduation_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                graduation_task.cancel()
                try:
                    await graduation_task
                except (asyncio.CancelledError, Exception):
                    pass
        if ivykids_sync_task is not None:
            if ivykids_sync_stop_event is not None:
                ivykids_sync_stop_event.set()
            try:
                await asyncio.wait_for(ivykids_sync_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                ivykids_sync_task.cancel()
                try:
                    await ivykids_sync_task
                except (asyncio.CancelledError, Exception):
                    pass
        if salary_snapshot_task is not None:
            if salary_snapshot_stop_event is not None:
                salary_snapshot_stop_event.set()
            try:
                await asyncio.wait_for(salary_snapshot_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                salary_snapshot_task.cancel()
                try:
                    await salary_snapshot_task
                except (asyncio.CancelledError, Exception):
                    pass
        if activity_waitlist_task is not None:
            if activity_waitlist_stop_event is not None:
                activity_waitlist_stop_event.set()
            try:
                await asyncio.wait_for(activity_waitlist_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                activity_waitlist_task.cancel()
                try:
                    await activity_waitlist_task
                except (asyncio.CancelledError, Exception):
                    pass
        if medication_reminder_task is not None:
            if medication_reminder_stop_event is not None:
                medication_reminder_stop_event.set()
            try:
                await asyncio.wait_for(medication_reminder_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                medication_reminder_task.cancel()
                try:
                    await medication_reminder_task
                except (asyncio.CancelledError, Exception):
                    pass
        if security_gc_task is not None:
            if security_gc_stop_event is not None:
                security_gc_stop_event.set()
            try:
                await asyncio.wait_for(security_gc_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                security_gc_task.cancel()
                try:
                    await security_gc_task
                except (asyncio.CancelledError, Exception):
                    pass
        if pii_retention_task is not None:
            if pii_retention_stop_event is not None:
                pii_retention_stop_event.set()
            try:
                await asyncio.wait_for(pii_retention_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pii_retention_task.cancel()
                try:
                    await pii_retention_task
                except (asyncio.CancelledError, Exception):
                    pass
        if official_calendar_task is not None:
            if official_calendar_stop_event is not None:
                official_calendar_stop_event.set()
            try:
                await asyncio.wait_for(official_calendar_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                official_calendar_task.cancel()
                try:
                    await official_calendar_task
                except (asyncio.CancelledError, Exception):
                    pass
        if finance_reconciliation_task is not None:
            if finance_reconciliation_stop_event is not None:
                finance_reconciliation_stop_event.set()
            try:
                await asyncio.wait_for(finance_reconciliation_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                finance_reconciliation_task.cancel()
                try:
                    await finance_reconciliation_task
                except (asyncio.CancelledError, Exception):
                    pass
        if data_quality_task is not None:
            if data_quality_stop_event is not None:
                data_quality_stop_event.set()
            try:
                await asyncio.wait_for(data_quality_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                data_quality_task.cancel()
                try:
                    await data_quality_task
                except (asyncio.CancelledError, Exception):
                    pass
        if recruitment_term_advance_task is not None:
            if recruitment_term_advance_stop_event is not None:
                recruitment_term_advance_stop_event.set()
            try:
                await asyncio.wait_for(recruitment_term_advance_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                recruitment_term_advance_task.cancel()
                try:
                    await recruitment_term_advance_task
                except (asyncio.CancelledError, Exception):
                    pass
        if leave_quota_expiry_task is not None:
            if leave_quota_expiry_stop_event is not None:
                leave_quota_expiry_stop_event.set()
            try:
                await asyncio.wait_for(leave_quota_expiry_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                leave_quota_expiry_task.cancel()
                try:
                    await leave_quota_expiry_task
                except (asyncio.CancelledError, Exception):
                    pass
        if line_retry_task is not None:
            if line_retry_stop_event is not None:
                line_retry_stop_event.set()
            try:
                await asyncio.wait_for(line_retry_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                line_retry_task.cancel()
                try:
                    await line_retry_task
                except (asyncio.CancelledError, Exception):
                    pass
        if pending_uploads_task is not None:
            if pending_uploads_stop_event is not None:
                pending_uploads_stop_event.set()
            try:
                await asyncio.wait_for(pending_uploads_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pending_uploads_task.cancel()
                try:
                    await pending_uploads_task
                except (asyncio.CancelledError, Exception):
                    pass
        if line_token_health_task is not None:
            if line_token_health_stop_event is not None:
                line_token_health_stop_event.set()
            try:
                await asyncio.wait_for(line_token_health_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                line_token_health_task.cancel()
                try:
                    await line_token_health_task
                except (asyncio.CancelledError, Exception):
                    pass
        # Graceful Shutdown：釋放資源
        logger.info("Application shutting down — releasing resources…")
        # PDF worker：等所有 in-flight 報告生成完（最多 shutdown_timeout 秒）
        # 必須在 DB 連線池釋放之前，否則 worker 寫 status 會炸
        try:
            from services import pdf_worker

            pdf_worker.shutdown(wait=True)
            logger.info("PDF worker executor 已釋放")
        except Exception as e:
            logger.warning("PDF worker shutdown 失敗: %s", e)
            capture_exception(e, level="warning")
        # 關閉所有 WebSocket 連線
        try:
            from api.dismissal_ws import manager as ws_manager

            for ws in list(ws_manager._admin_conns):
                try:
                    await ws.close(code=1001, reason="Server shutting down")
                except Exception:
                    pass
            ws_manager._admin_conns.clear()
            for classroom_conns in ws_manager._teacher_conns.values():
                for ws in list(classroom_conns):
                    try:
                        await ws.close(code=1001, reason="Server shutting down")
                    except Exception:
                        pass
                classroom_conns.clear()
            logger.info("WebSocket 連線已全部關閉")
        except Exception as e:
            logger.warning("WebSocket 關閉時發生錯誤: %s", e)
            capture_exception(e, level="warning")
        # 釋放 DB 連線池
        try:
            from models.base import get_engine as _get_engine

            _get_engine().dispose()
            logger.info("資料庫連線池已釋放")
        except Exception as e:
            logger.warning("資料庫連線池釋放失敗: %s", e)
            capture_exception(e, level="warning")
        logger.info("Application shutdown complete.")


# 環境判斷需早於 FastAPI() 建構，docs/redoc/openapi 在 prod 預設關閉以避免
# 完整 router/schema/權限欄位地圖被未認證者抓走（攻擊面地圖洩漏）。
_env_name = settings.core.env.lower()
_is_prod_env = settings.core.is_production
_cors_origins = settings.network.cors_origins
_docs_force_enable = settings.core.enable_api_docs
_docs_enabled = _docs_force_enable or not _is_prod_env

app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="2.0.0",
    lifespan=app_lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# 全域 exception handler：envelope 化 BusinessError / 422 / unhandled；
# HTTPException 透傳原 detail shape（保 943 處 inline 與既有測試 assertion 相容）。
from utils.exception_handlers import register_exception_handlers

register_exception_handlers(app)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

if _cors_origins:
    CORS_ORIGINS = _cors_origins
elif _is_prod_env:
    raise RuntimeError("CORS_ORIGINS 環境變數未設定，正式環境不允許使用開發預設來源。")
else:
    CORS_ORIGINS = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "If-Match"],
)

# ---------------------------------------------------------------------------
# Service Injection
# ---------------------------------------------------------------------------

init_salary_services(salary_engine, insurance_service, line_service)
init_employee_services(salary_engine)
init_config_services(salary_engine, line_service)
init_insurance_services(insurance_service)
init_overtimes_services(salary_engine)
init_leaves_services(salary_engine)
# Phase 2 PR-D (2026-05-26): leaves / overtimes / punch_corrections / announcements
# / activity 的 _line_service injection 已退役（caller 改走 dispatch.enqueue）。
# 仍保留：dismissal / growth_reports / portal contact_book hybrid + 家長 LIFF /
# webhook / config / salary engine 內部使用。
init_dismissal_line_service(line_service)
init_portal_notify_services(line_service)
init_growth_reports_line_service(line_service)
init_webhook_service(line_service)
init_comp_grant_reminder_line_service(line_service)
init_ops_alert_service(line_service)
init_gov_report_services(insurance_service)
init_bonus_preview_services(salary_engine)
init_parent_line_service(line_login_service)

# Ensure data directories exist
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)
# 上傳檔案實體儲存路徑（集中在 data/uploads/，可用 STORAGE_ROOT 覆寫）
from utils.storage import get_storage_path  # noqa: E402

get_storage_path("leave_attachments")
get_storage_path("activity_posters")
get_storage_path("attendance_imports")

# ---------------------------------------------------------------------------
# Register Routers
# ---------------------------------------------------------------------------

app.include_router(employees_router)
app.include_router(employees_docs_router)
app.include_router(students_router)
app.include_router(student_attendance_router)
app.include_router(student_incidents_router)
app.include_router(student_assessments_router)
app.include_router(classrooms_router)
app.include_router(vendor_payments_router)
app.include_router(monthly_fixed_costs_router)
app.include_router(attendance_router)
app.include_router(salary_router)
app.include_router(disciplinary_router)
app.include_router(art_teacher_payroll_router)
app.include_router(system_config_router)
app.include_router(config_router)
app.include_router(leaves_router)
app.include_router(overtimes_router)
app.include_router(insurance_router)
app.include_router(auth_router)
app.include_router(portal_router)
app.include_router(shifts_router)
app.include_router(events_router)
app.include_router(meetings_router)
app.include_router(announcements_router)
app.include_router(approvals_router)
app.include_router(notifications_router)
app.include_router(reports_router)
from api.analytics import router as analytics_router

app.include_router(analytics_router)
app.include_router(exports_router)
app.include_router(audit_router)
app.include_router(internal_metrics_router)
app.include_router(integrations_health_router)
if settings.core.dev_router_should_mount:
    from api.dev import router as dev_router, init_dev_services

    init_dev_services(salary_engine)
    app.include_router(dev_router)
    logger.warning("Dev router 已掛載（/api/dev/*），正式環境請設定 ENV=production")
app.include_router(punch_corrections_router)
app.include_router(approval_settings_router)
app.include_router(activity_router)
app.include_router(dismissal_calls_router)
app.include_router(dismissal_ws_router)  # WebSocket（路徑已含 /ws/...）
app.include_router(contact_book_ws_router)  # 聯絡簿 WebSocket（教師 + 家長 channel）
app.include_router(line_webhook_router)
app.include_router(gov_reports_router)
app.include_router(gov_moe_router, prefix="/api")
app.include_router(fees_router)
app.include_router(academic_terms_router)
app.include_router(recruitment_router)
app.include_router(recruitment_ivykids_router)
app.include_router(recruitment_gov_kindergartens_router)
app.include_router(student_enrollment_router)
app.include_router(student_change_logs_router)
app.include_router(student_communications_router)
app.include_router(bonus_preview_router)
app.include_router(health_router)
# Portfolio / 幼兒成長歷程（Batch A）
app.include_router(attachments_router)
app.include_router(attachments_download_router)
app.include_router(observations_router)
app.include_router(measurements_router)
app.include_router(milestones_router)
app.include_router(timeline_router)
app.include_router(auto_milestone_router)
app.include_router(growth_reports_router)
app.include_router(student_attachments_router)
app.include_router(student_health_router)
# 家長入口
app.include_router(parent_portal_router)
app.include_router(parent_admin_router)
app.include_router(student_leaves_router)
app.include_router(appraisal_router)
app.include_router(year_end_router)
app.include_router(calendar_admin_router, prefix="/api/calendar")
from api.permissions_admin import router as permissions_admin_router

app.include_router(permissions_admin_router)
app.include_router(offboarding_router, prefix="/api")

from api import leave_quota_expiry as _lqe_api

app.include_router(_lqe_api.router, prefix="/api")

# ---------------------------------------------------------------------------
# Middleware（順序重要：最後加入的最先執行）
# ---------------------------------------------------------------------------

from utils.audit import AuditMiddleware

app.add_middleware(AuditMiddleware)

from utils.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)

from utils.request_logging import RequestLoggingMiddleware

app.add_middleware(RequestLoggingMiddleware)

# TrustedHost 必須最後 add（成為最外層），在所有處理前先驗證 Host header。
# 防 Host header injection / 開放重新導向 / 快取毒化。
from fastapi.middleware.trustedhost import TrustedHostMiddleware

_allowed_hosts = settings.network.allowed_hosts
if _allowed_hosts:
    ALLOWED_HOSTS = _allowed_hosts
elif _is_prod_env:
    raise RuntimeError(
        "ALLOWED_HOSTS 環境變數未設定，正式環境必須明列允許的 Host header（防 Host header 攻擊）。"
    )
else:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver", "*.zeabur.app"]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn

    # 反向代理感知：在 LB / nginx / cloudflare 後面時，讓 uvicorn 從
    # X-Forwarded-For / Forwarded 標頭重寫 request.client.host。
    # forwarded_allow_ips 可由 TRUSTED_PROXY_IPS 控制；預設 "*" 接受任何
    # 前代理（dev 友善）。Prod 應設為 LB 內網 IP / CIDR。
    forwarded_allow = settings.network.trusted_proxy_ips
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow,
    )
