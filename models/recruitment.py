"""
models/recruitment.py — 招生訪視記錄
"""

from datetime import datetime, date
from utils.taipei_time import now_taipei_naive
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    Text,
    Index,
    Float,
    JSON,
    ForeignKey,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base


class RecruitmentVisit(Base):
    """招生訪視記錄表"""

    __tablename__ = "recruitment_visits"

    id = Column(Integer, primary_key=True, index=True)
    month = Column(String(10), nullable=False, index=True)  # 民國月份，如 "115.03"
    seq_no = Column(String(10), nullable=True)  # 月份內序號
    visit_date = Column(String(50), nullable=True)  # 原始日期字串（含備注）
    child_name = Column(String(50), nullable=False)  # 幼生姓名
    birthday = Column(Date, nullable=True)  # 生日
    grade = Column(String(20), nullable=True)  # 適讀班級
    phone = Column(String(100), nullable=True)  # 電話
    address = Column(String(200), nullable=True)  # 地址
    district = Column(String(30), nullable=True, index=True)  # 行政區
    source = Column(String(50), nullable=True, index=True)  # 幼生來源
    referrer = Column(String(50), nullable=True, index=True)  # 介紹者
    deposit_collector = Column(String(50), nullable=True)  # 收預繳人員
    has_deposit = Column(Boolean, default=False, nullable=False)  # 是否預繳
    notes = Column(Text, nullable=True)  # 備註（含預計就讀月份）
    parent_response = Column(Text, nullable=True)  # 電訪後家長回應
    geocoding_consent_at = Column(DateTime, nullable=True)  # PII consent attestation

    # --- 延伸欄位（Excel 未預繳原因分析 / 近五年追蹤） ---
    no_deposit_reason = Column(String(60), nullable=True)  # 未預繳原因分類
    no_deposit_reason_detail = Column(Text, nullable=True)  # 未預繳判定說明
    enrolled = Column(Boolean, default=False, nullable=False)  # 是否已實際報到/註冊
    transfer_term = Column(Boolean, default=False, nullable=False)  # 是否轉到其他學期
    expected_start_label = Column(
        String(30), nullable=True
    )  # 預計就讀月份標籤，create/update 時自動計算

    # --- 暫定編班（保留座位；綁年級+目標學年，不綁具體班級 row） ---
    provisional_grade_id = Column(
        Integer, ForeignKey("class_grades.id", ondelete="SET NULL"), nullable=True
    )  # 暫定年級
    target_school_year = Column(Integer, nullable=True)  # 目標學年（民國，如 115）
    target_semester = Column(Integer, nullable=True, default=1)  # 目標學期（1=上）

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        Index("ix_recruitment_month_grade", "month", "grade"),
        Index("ix_rv_has_deposit", "has_deposit"),
        Index("ix_rv_no_deposit_reason", "no_deposit_reason"),
        Index("ix_rv_has_deposit_grade", "has_deposit", "grade"),
        Index("ix_rv_source_grade", "source", "grade"),
        Index("ix_rv_referrer_grade", "referrer", "grade"),
        Index("ix_rv_month_has_deposit", "month", "has_deposit"),
        Index("ix_rv_expected_start_label", "expected_start_label"),
        Index(
            "ix_rv_target_grade",
            "target_school_year",
            "target_semester",
            "provisional_grade_id",
        ),
    )


class RecruitmentIvykidsRecord(Base):
    """義華校官網同步報名資料表。"""

    __tablename__ = "recruitment_ivykids_records"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String(100), nullable=False, unique=True, index=True)
    external_status = Column(String(50), nullable=True)
    external_created_at = Column(String(50), nullable=True)
    month = Column(String(10), nullable=False, index=True)
    visit_date = Column(String(50), nullable=True)
    child_name = Column(String(50), nullable=False)
    birthday = Column(Date, nullable=True)
    grade = Column(String(20), nullable=True)
    phone = Column(String(100), nullable=True)
    address = Column(String(200), nullable=True)
    district = Column(String(30), nullable=True, index=True)
    source = Column(String(50), nullable=True, index=True)
    referrer = Column(String(50), nullable=True)
    deposit_collector = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    parent_response = Column(Text, nullable=True)
    has_deposit = Column(Boolean, default=False, nullable=False)
    enrolled = Column(Boolean, default=False, nullable=False)
    transfer_term = Column(Boolean, default=False, nullable=False)
    geocoding_consent_at = Column(DateTime, nullable=True)  # PII consent attestation
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (Index("ix_recruitment_ivykids_month_source", "month", "source"),)


class RecruitmentMonth(Base):
    """手動登記的招生月份（補充訪視記錄中未出現的月份）"""

    __tablename__ = "recruitment_months"

    id = Column(Integer, primary_key=True, index=True)
    month = Column(String(10), nullable=False, unique=True)  # 民國月份，如 "115.04"
    created_at = Column(DateTime, default=now_taipei_naive)


class RecruitmentGeocodeCache(Base):
    """招生地址 geocoding 快取，避免重複打外部 API。"""

    __tablename__ = "recruitment_geocode_cache"

    id = Column(Integer, primary_key=True, index=True)
    address = Column(String(200), nullable=False, unique=True, index=True)
    district = Column(String(30), nullable=True)
    formatted_address = Column(String(255), nullable=True)
    matched_address = Column(String(255), nullable=True)
    google_place_id = Column(Text, nullable=True)
    provider = Column(String(20), nullable=True)
    status = Column(
        String(20), nullable=False, default="pending"
    )  # pending/resolved/failed
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    town_code = Column(String(20), nullable=True, index=True)
    town_name = Column(String(50), nullable=True)
    county_name = Column(String(50), nullable=True)
    land_use_label = Column(String(120), nullable=True)
    travel_minutes = Column(Float, nullable=True)
    travel_distance_km = Column(Float, nullable=True)
    data_quality = Column(String(20), nullable=False, default="partial")
    error_message = Column(String(255), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class RecruitmentCampusSetting(Base):
    """招生生活圈分析主園所設定。v1 僅使用單筆資料。"""

    __tablename__ = "recruitment_campus_settings"

    id = Column(Integer, primary_key=True, index=True)
    campus_name = Column(String(100), nullable=False, default="本園")
    campus_address = Column(String(255), nullable=False, default="")
    campus_lat = Column(Float, nullable=True)
    campus_lng = Column(Float, nullable=True)
    travel_mode = Column(String(20), nullable=False, default="driving")
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class RecruitmentAreaInsightCache(Base):
    """行政區 / 鄉鎮市區層級的市場情報快取。"""

    __tablename__ = "recruitment_area_insight_cache"

    id = Column(Integer, primary_key=True, index=True)
    county_name = Column(String(50), nullable=True)
    district = Column(String(50), nullable=False, index=True)
    town_code = Column(String(20), nullable=True, unique=True, index=True)
    population_density = Column(Float, nullable=True)
    population_0_6 = Column(Integer, nullable=True)
    data_completeness = Column(String(20), nullable=False, default="partial")
    source_notes = Column(Text, nullable=True)
    synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class RecruitmentSyncState(Base):
    """外部招生資料同步狀態。"""

    __tablename__ = "recruitment_sync_states"

    id = Column(Integer, primary_key=True, index=True)
    provider_name = Column(String(50), nullable=False, unique=True, index=True)
    provider_label = Column(String(100), nullable=True)
    sync_in_progress = Column(Boolean, default=False, nullable=False)
    last_started_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String(20), nullable=True)
    last_sync_message = Column(Text, nullable=True)
    last_sync_counts = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class RecruitmentPeriod(Base):
    """近五年招生期間轉換整合表（每期半年一筆）"""

    __tablename__ = "recruitment_periods"

    id = Column(Integer, primary_key=True, index=True)
    period_name = Column(
        String(50), nullable=False, unique=True
    )  # 如 "114.09.16~115.03.15"
    visit_count = Column(Integer, default=0)  # 參觀人數
    deposit_count = Column(Integer, default=0)  # 預繳人數
    enrolled_count = Column(Integer, default=0)  # 實際註冊人數
    transfer_term_count = Column(Integer, default=0)  # 轉到其他學期
    effective_deposit_count = Column(Integer, default=0)  # 有效預繳（預繳 - 轉期）
    not_enrolled_deposit = Column(Integer, default=0)  # 未就讀退預繳
    enrolled_after_school = Column(Integer, default=0)  # 註冊後退學
    notes = Column(Text, nullable=True)  # 備註
    sort_order = Column(Integer, default=0)  # 排序
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class CompetitorSchool(Base):
    """教育部幼兒園基本資料快取（competitor_school 表 ORM 對應）。"""

    __tablename__ = "competitor_school"

    id = Column(Integer, primary_key=True, index=True)
    source_school_id = Column(String(100), nullable=False, unique=True, index=True)
    source_key = Column(String(120), nullable=True, unique=True, index=True)
    school_name = Column(String(255), nullable=False, index=True)
    owner_name = Column(String(255), nullable=True)
    school_type = Column(
        String(50), nullable=True, index=True
    )  # 設立別：公立/私立/非營利
    pre_public_type = Column(String(50), nullable=True)  # 準公共幼兒園：有/無
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    phone = Column(String(50), nullable=True)
    website = Column(String(500), nullable=True)
    city = Column(String(50), nullable=True)
    district = Column(String(50), nullable=True)
    address = Column(String(500), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    approved_capacity = Column(Integer, nullable=True)  # 核定人數
    approved_date = Column(String(20), nullable=True)  # 核准設立日期
    total_area_sqm = Column(Float, nullable=True)  # 全園總面積（平方公尺）
    monthly_fee = Column(Integer, nullable=True)
    has_penalty = Column(Boolean, nullable=False, default=False, index=True)
    source_updated_at = Column(DateTime, nullable=True)

    # Google 比對快取
    google_place_id = Column(String(255), nullable=True, index=True)
    google_name = Column(Text, nullable=True)
    google_rating = Column(Float, nullable=True)
    google_rating_count = Column(Integer, nullable=True)
    google_maps_uri = Column(Text, nullable=True)
    google_matched_at = Column(DateTime, nullable=True)
    match_confidence = Column(Integer, nullable=True)

    # kiang 補充欄位
    indoor_area_sqm = Column(Float, nullable=True)
    outdoor_area_sqm = Column(Float, nullable=True)
    floor_info = Column(String(255), nullable=True)
    shuttle_info = Column(String(255), nullable=True)
    has_after_school = Column(Boolean, default=False)
    kiang_synced_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (Index("idx_competitor_city_district", "city", "district"),)


class RecruitmentEventLog(Base):
    """招生漏斗階段事件流（visit 層級的 timeline）。

    PG 用 JSONB；SQLite（測試）退化為 JSON。
    """

    __tablename__ = "recruitment_event_log"

    id = Column(Integer, primary_key=True, index=True)
    recruitment_visit_id = Column(
        Integer,
        ForeignKey("recruitment_visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type = Column(String(40), nullable=False)
    from_stage = Column(String(20), nullable=True)
    to_stage = Column(String(20), nullable=False)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason = Column(Text, nullable=True)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # PG=JSONB / SQLite=JSON（with_variant pattern，見 models/appraisal.py）
    metadata_json = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (
        Index(
            "ix_recruitment_event_log_visit_time", "recruitment_visit_id", "created_at"
        ),
        Index("ix_recruitment_event_log_event_type", "event_type"),
        Index("ix_recruitment_event_log_actor", "actor_user_id"),
    )


class GradeIntakeTarget(Base):
    """各年級各學年的招生「計畫名額」（名額規劃面板的 target 來源）。"""

    __tablename__ = "grade_intake_targets"

    id = Column(Integer, primary_key=True, index=True)
    grade_id = Column(
        Integer, ForeignKey("class_grades.id", ondelete="CASCADE"), nullable=False
    )
    school_year = Column(Integer, nullable=False)  # 民國學年
    semester = Column(Integer, nullable=False, default=1, server_default=text("1"))
    target_seats = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        Index(
            "uq_grade_intake_target",
            "grade_id",
            "school_year",
            "semester",
            unique=True,
        ),
    )
