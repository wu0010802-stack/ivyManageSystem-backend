"""
startup/bootstrap.py — 應用程式啟動編排

將 seed、migration、服務初始化等步驟統一呼叫。
"""

import logging

import sqlalchemy
from sqlalchemy import inspect as sa_inspect

from models.database import get_engine, get_session, init_database, LineConfig
from startup.seed import (
    seed_class_grades,
    seed_job_titles,
    seed_default_configs,
    seed_shift_types,
    seed_default_admin,
    seed_approval_policies,
    seed_activity_settings,
)
from startup.migrations import (
    run_alembic_upgrade,
    migrate_school_year_to_roc,
    migrate_permissions_rw,
)

logger = logging.getLogger(__name__)


def _load_line_config(line_service):
    """啟動時從 DB 載入 LINE 通知設定"""
    session = get_session()
    try:
        cfg = session.query(LineConfig).first()
        if cfg and cfg.is_enabled and cfg.channel_access_token and cfg.target_id:
            channel_secret = getattr(cfg, "channel_secret", None)
            line_service.configure(
                cfg.channel_access_token, cfg.target_id, True, channel_secret
            )
            logger.info("LINE 通知服務已啟用")
        else:
            logger.info("LINE 通知服務未啟用或尚未設定")
    finally:
        session.close()


def run_startup_bootstrap(salary_engine, line_service):
    """執行啟動必要任務，不包含 schema/data migration。"""
    from models.recruitment import (
        RecruitmentVisit,
        RecruitmentIvykidsRecord,
        RecruitmentPeriod,
        RecruitmentMonth,
        RecruitmentGeocodeCache,
        RecruitmentCampusSetting,
        RecruitmentAreaInsightCache,
        RecruitmentSyncState,
    )
    from models.fees import FeeItem, StudentFeeRecord
    from models.student_log import StudentChangeLog
    from models.config import PositionSalaryConfig

    init_database()

    # 安裝慢查詢監控
    from utils.slow_query_logger import install_slow_query_logger

    install_slow_query_logger(get_engine())

    # 確保 position_salary_configs 表存在
    _engine = get_engine()
    PositionSalaryConfig.__table__.create(_engine, checkfirst=True)

    # position_salary_configs 新增 director / principal 欄位（幂等）
    inspector = sa_inspect(_engine)
    columns = [c["name"] for c in inspector.get_columns("position_salary_configs")]
    for _col, _ddl in [
        (
            "director",
            "ALTER TABLE position_salary_configs ADD COLUMN director FLOAT",
        ),
        (
            "principal",
            "ALTER TABLE position_salary_configs ADD COLUMN principal FLOAT",
        ),
    ]:
        if _col not in columns:
            with _engine.connect() as _conn:
                try:
                    _conn.execute(sqlalchemy.text(_ddl))
                    _conn.commit()
                    logger.info("position_salary_configs: 已新增欄位 %s", _col)
                except Exception as e:
                    logger.error("新增欄位 %s 失敗: %s", _col, e)
    engine = get_engine()
    RecruitmentVisit.__table__.create(engine, checkfirst=True)
    RecruitmentIvykidsRecord.__table__.create(engine, checkfirst=True)
    RecruitmentPeriod.__table__.create(engine, checkfirst=True)
    RecruitmentMonth.__table__.create(engine, checkfirst=True)
    RecruitmentGeocodeCache.__table__.create(engine, checkfirst=True)
    RecruitmentCampusSetting.__table__.create(engine, checkfirst=True)
    RecruitmentAreaInsightCache.__table__.create(engine, checkfirst=True)
    RecruitmentSyncState.__table__.create(engine, checkfirst=True)
    FeeItem.__table__.create(engine, checkfirst=True)
    StudentFeeRecord.__table__.create(engine, checkfirst=True)
    StudentChangeLog.__table__.create(engine, checkfirst=True)
    migrate_school_year_to_roc()
    seed_class_grades()
    seed_job_titles()
    seed_default_configs()
    seed_shift_types()
    seed_default_admin()
    seed_approval_policies()
    seed_activity_settings()
    salary_engine.load_config_from_db()
    _load_line_config(line_service)
    from api.recruitment import normalize_existing_months

    normalize_existing_months()


def run_maintenance_tasks():
    """執行部署/維運任務：schema migration 與資料回填。"""
    run_alembic_upgrade()
    migrate_school_year_to_roc()
    migrate_permissions_rw()
