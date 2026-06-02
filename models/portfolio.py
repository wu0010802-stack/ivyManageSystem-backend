"""
models/portfolio.py — 幼兒成長歷程 Portfolio 模組

包含：
- Attachment                 多型附件（owner_type + owner_id 指向 observation / report / medication_order）
- StudentObservation         日常正向觀察（與 StudentIncident 並存，專記學習亮點與里程碑）
- StudentAllergy             長期過敏資訊（結構化取代 Student.allergy 純文字欄位）
- StudentMedicationOrder     當日臨時用藥單（一張 order 僅生效一天）
- StudentMedicationLog       餵藥執行紀錄（append-only；已 administered/skipped 者不可修改，
                             改透過 correction_of 新增一筆修正紀錄）
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)

from models.base import Base
from utils.medical_field_type import EncryptedText

# ── Attachment owner_type 枚舉 ────────────────────────────────────────────
ATTACHMENT_OWNER_OBSERVATION = "observation"
ATTACHMENT_OWNER_REPORT = "report"
ATTACHMENT_OWNER_MEDICATION_ORDER = "medication_order"
ATTACHMENT_OWNER_MESSAGE = "message"
ATTACHMENT_OWNER_EVENT_ACK = "event_acknowledgment"
ATTACHMENT_OWNER_STUDENT_LEAVE = "student_leave"
ATTACHMENT_OWNER_CONTACT_BOOK = "contact_book_entry"
ATTACHMENT_OWNER_ANNOUNCEMENT = "announcement"
ATTACHMENT_OWNER_TYPES = (
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_REPORT,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_MESSAGE,
    ATTACHMENT_OWNER_EVENT_ACK,
    ATTACHMENT_OWNER_STUDENT_LEAVE,
    ATTACHMENT_OWNER_CONTACT_BOOK,
    ATTACHMENT_OWNER_ANNOUNCEMENT,
)

# ── 觀察 domain 枚舉（沿用 StudentAssessment.domain） ──────────────────────
OBSERVATION_DOMAINS = (
    "身體動作與健康",
    "語文",
    "認知",
    "社會",
    "情緒",
    "美感",
    "綜合",
)

# ── 過敏嚴重度 ────────────────────────────────────────────────────────────
ALLERGY_SEVERITY_MILD = "mild"
ALLERGY_SEVERITY_MODERATE = "moderate"
ALLERGY_SEVERITY_SEVERE = "severe"
ALLERGY_SEVERITIES = (
    ALLERGY_SEVERITY_MILD,
    ALLERGY_SEVERITY_MODERATE,
    ALLERGY_SEVERITY_SEVERE,
)

# ── Medication order source ──────────────────────────────────────────────
MEDICATION_SOURCE_TEACHER = "teacher"
MEDICATION_SOURCE_PARENT = "parent"  # 未來 portal 家長自填
MEDICATION_SOURCES = (MEDICATION_SOURCE_TEACHER, MEDICATION_SOURCE_PARENT)


class Attachment(Base):
    """多型附件：同一張表掛載於 observation / report / medication_order。

    storage_key 為 portfolio_storage 回傳的相對路徑（如 "2026/04/{uuid}.jpg"）；
    display_key / thumb_key 僅影像有值，影片兩者為 NULL。
    """

    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_type = Column(String(30), nullable=False)
    owner_id = Column(Integer, nullable=False)
    storage_key = Column(String(255), nullable=False)
    display_key = Column(String(255), nullable=True)
    thumb_key = Column(String(255), nullable=True)
    original_filename = Column(String(255), nullable=False)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    deleted_at = Column(
        DateTime, nullable=True, comment="軟刪除；實際檔案保留 90 天後再清"
    )
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (
        Index("ix_attachments_owner", "owner_type", "owner_id"),
        Index("ix_attachments_uploaded_by", "uploaded_by"),
        Index("ix_attachments_deleted_at", "deleted_at"),
    )


class StudentObservation(Base):
    """學生日常正向觀察（學習亮點、里程碑、有趣言行、社交互動）。

    與 StudentIncident 並存：Incident 記錄異常事件，Observation 記錄日常學習。
    """

    __tablename__ = "student_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    observation_date = Column(Date, nullable=False)
    domain = Column(
        String(30),
        nullable=True,
        comment="發展領域：對齊 StudentAssessment.domain 枚舉",
    )
    narrative = Column(Text, nullable=False)
    rating = Column(
        SmallInteger,
        nullable=True,
        comment="1-5 星；nullable（不強制打分）",
    )
    is_highlight = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="是否為成長里程碑（納入學期報告精選）",
    )
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    deleted_at = Column(DateTime, nullable=True, comment="軟刪除")

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        Index("ix_student_observations_student_date", "student_id", "observation_date"),
        Index("ix_student_observations_highlight", "is_highlight"),
        Index("ix_student_observations_deleted_at", "deleted_at"),
    )


class StudentAllergy(Base):
    """學生長期過敏資訊（結構化版本）。

    學生主檔卡片與點名頁需依 (student_id, active=true) 顯示紅色 badge。
    """

    __tablename__ = "student_allergies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    # RA-MED-10：§6 特種個資（醫療）— ORM 透明 Fernet 加密（DB 仍 Text）。
    # legacy 明文 passthrough（decrypt_medical 對非 Fernet token 原樣回）。
    allergen = Column(
        EncryptedText, nullable=False, comment="過敏原，如 花生 / 乳製品 / 塵蟎（加密）"
    )
    # severity 維持明文：parent_portal/profile.py 以 DB-level ORDER BY severity.desc()
    # 排序（severe>moderate>mild 字母序剛好對），加密會破壞此功能且為低敏感 3 值列舉。
    severity = Column(
        String(10),
        nullable=False,
        comment="mild / moderate / severe（明文，DB 排序需求）",
    )
    reaction_symptom = Column(
        EncryptedText, nullable=True, comment="過敏反應症狀（加密）"
    )
    first_aid_note = Column(
        EncryptedText, nullable=True, comment="急救處置說明（加密）"
    )
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        Index("ix_student_allergies_student_active", "student_id", "active"),
    )


class StudentMedicationOrder(Base):
    """學生當日臨時用藥單。

    一張 order 僅限當日（order_date 為 unique 觸發點）；建立時會自動依 time_slots
    預建 N 筆 StudentMedicationLog（狀態 pending）供老師勾選執行。
    """

    __tablename__ = "student_medication_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    order_date = Column(Date, nullable=False, comment="生效日，一張 order 僅限當日")
    medication_name = Column(String(100), nullable=False)
    dose = Column(String(50), nullable=False, comment="劑量，如 1 顆 / 5ml")
    time_slots = Column(
        JSON,
        nullable=False,
        comment='時段陣列，如 ["08:30", "12:00", "15:00"]',
    )
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    source = Column(
        String(20),
        nullable=False,
        default=MEDICATION_SOURCE_TEACHER,
        server_default=MEDICATION_SOURCE_TEACHER,
        comment="teacher / parent（未來 portal 家長填）",
    )

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        Index("ix_medication_orders_student_date", "student_id", "order_date"),
        Index("ix_medication_orders_date", "order_date"),
    )


class StudentMedicationLog(Base):
    """餵藥執行紀錄（append-only / immutable after administered）。

    立下不可變規則：
    - 一旦 `administered_at IS NOT NULL` 或 `skipped = true`，該筆 log 不可 UPDATE
      （由 migration 建立 DB trigger 拒絕 UPDATE）
    - 修正請透過 `POST /api/medication-logs/{id}/correct`，新增一筆 correction log，
      `correction_of` 指向原 log；原 log 保持不變
    """

    __tablename__ = "student_medication_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer,
        ForeignKey("student_medication_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_time = Column(String(5), nullable=False, comment='時段，如 "08:30"')
    administered_at = Column(DateTime, nullable=True, comment="null = pending")
    administered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    skipped = Column(Boolean, nullable=False, default=False, server_default="false")
    skipped_reason = Column(String(200), nullable=True)
    note = Column(String(200), nullable=True)
    correction_of = Column(
        Integer,
        ForeignKey("student_medication_logs.id", ondelete="SET NULL"),
        nullable=True,
        comment="若為修正紀錄，指向被修正的原 log",
    )

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (
        # 每張 order 的每個時段只能預建「一筆原始 log」（correction_of IS NULL）
        # 修正 log（correction_of IS NOT NULL）允許同時段多筆
        # 使用 partial unique index；SQLite 測試環境會退化為一般 unique（可接受）
        Index(
            "uq_medication_logs_order_slot_primary",
            "order_id",
            "scheduled_time",
            unique=True,
            postgresql_where=text("correction_of IS NULL"),
            sqlite_where=text("correction_of IS NULL"),
        ),
        Index("ix_medication_logs_administered_at", "administered_at"),
        Index("ix_medication_logs_correction_of", "correction_of"),
    )


# ── Milestone type 枚舉 ───────────────────────────────────────────────────
MILESTONE_TYPE_BIRTHDAY = "birthday"
MILESTONE_TYPE_FIRST_DAY = "first_day"
MILESTONE_TYPE_PERFECT_ATTENDANCE_MONTH = "perfect_attendance_month"
MILESTONE_TYPE_FIRST_SOLO_EVENT = "first_solo_event"
MILESTONE_TYPE_ASSESSMENT_EXCELLENCE = "assessment_excellence"
MILESTONE_TYPE_ACTIVITY_FIRST_JOIN = "activity_first_join"
MILESTONE_TYPE_GRADUATION = "graduation"
MILESTONE_TYPE_CUSTOM = "custom"
MILESTONE_TYPES = (
    MILESTONE_TYPE_BIRTHDAY,
    MILESTONE_TYPE_FIRST_DAY,
    MILESTONE_TYPE_PERFECT_ATTENDANCE_MONTH,
    MILESTONE_TYPE_FIRST_SOLO_EVENT,
    MILESTONE_TYPE_ASSESSMENT_EXCELLENCE,
    MILESTONE_TYPE_ACTIVITY_FIRST_JOIN,
    MILESTONE_TYPE_GRADUATION,
    MILESTONE_TYPE_CUSTOM,
)

# ── Milestone source 枚舉 ─────────────────────────────────────────────────
MILESTONE_SOURCE_MANUAL = "manual"
MILESTONE_SOURCE_AUTO_ATTENDANCE = "auto_attendance"
MILESTONE_SOURCE_AUTO_OBSERVATION = "auto_observation"
MILESTONE_SOURCE_AUTO_ASSESSMENT = "auto_assessment"
MILESTONE_SOURCE_AUTO_ENROLLMENT = "auto_enrollment"
MILESTONE_SOURCES = (
    MILESTONE_SOURCE_MANUAL,
    MILESTONE_SOURCE_AUTO_ATTENDANCE,
    MILESTONE_SOURCE_AUTO_OBSERVATION,
    MILESTONE_SOURCE_AUTO_ASSESSMENT,
    MILESTONE_SOURCE_AUTO_ENROLLMENT,
)

# ── Milestone reaction（家長端互動）──────────────────────────────────────
MILESTONE_REACTION_LIKE = "like"
MILESTONE_REACTION_LOVE = "love"
MILESTONE_REACTION_CELEBRATE = "celebrate"
MILESTONE_REACTIONS = (
    MILESTONE_REACTION_LIKE,
    MILESTONE_REACTION_LOVE,
    MILESTONE_REACTION_CELEBRATE,
)


class StudentMeasurement(Base):
    """學生量測紀錄：身高、體重、視力、頭圍。

    特性：
    - 至少一個量測值必填（DB CheckConstraint）
    - 無 soft delete；純數據可硬刪
    - 學生 CASCADE 刪除時量測紀錄一併消失
    """

    __tablename__ = "student_measurements"

    id = Column(Integer, primary_key=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    measured_on = Column(Date, nullable=False)
    height_cm = Column(Numeric(5, 2), nullable=True)
    weight_kg = Column(Numeric(5, 2), nullable=True)
    head_circumference_cm = Column(Numeric(5, 2), nullable=True)
    vision_left = Column(Numeric(3, 2), nullable=True)
    vision_right = Column(Numeric(3, 2), nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at = Column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_student_measurements_student_date",
            "student_id",
            "measured_on",
        ),
    )


class StudentMilestone(Base):
    """學生結構化里程碑。

    與 StudentObservation.is_highlight 並存：
    - is_highlight：教師對「日常觀察」打的亮點旗標
    - milestone：結構化里程碑（生日/全勤/首次活動等）

    特性：
    - soft delete（家長可能已 acknowledge / react，要保留軌跡）
    - 自動觸發 milestone 透過 (student, type, date, source, ref) 唯一鍵防重複
    - parent_reaction 取值受 MILESTONE_REACTIONS 限制（應用層 validate）
    """

    __tablename__ = "student_milestones"

    id = Column(Integer, primary_key=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    milestone_type = Column(String(40), nullable=False)
    achieved_on = Column(Date, nullable=False)
    title = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(40), nullable=True)

    source_type = Column(String(30), nullable=False, server_default=text("'manual'"))
    source_ref_type = Column(String(30), nullable=True)
    source_ref_id = Column(Integer, nullable=True)

    created_by = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at = Column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)

    parent_acknowledged_at = Column(DateTime, nullable=True)
    parent_acknowledged_by = Column(
        Integer,
        ForeignKey("guardians.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_reaction = Column(String(10), nullable=True)

    __table_args__ = (
        Index(
            "ix_student_milestones_student_date",
            "student_id",
            "achieved_on",
        ),
    )


# ── Growth report status 枚舉 ────────────────────────────────────────────
REPORT_STATUS_PENDING = "pending"
REPORT_STATUS_GENERATING = "generating"
REPORT_STATUS_READY = "ready"
REPORT_STATUS_FAILED = "failed"
REPORT_STATUSES = (
    REPORT_STATUS_PENDING,
    REPORT_STATUS_GENERATING,
    REPORT_STATUS_READY,
    REPORT_STATUS_FAILED,
)


class StudentGrowthReport(Base):
    """學生期末成長報告（PDF）追蹤表.

    每筆對應一份生成中或已完成的 PDF。
    - status 由 pending → generating → ready / failed
    - file_path 為相對於 instance/growth_reports/ 的路徑
    - parent_view_count 統計家長下載次數
    """

    __tablename__ = "student_growth_reports"

    id = Column(Integer, primary_key=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_label = Column(String(40), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    file_path = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    generated_by = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    generated_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    line_sent_at = Column(DateTime, nullable=True)
    parent_first_viewed_at = Column(DateTime, nullable=True)
    parent_view_count = Column(Integer, nullable=False, server_default=text("0"))

    teacher_narrative = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_growth_reports_student_period",
            "student_id",
            "period_start",
            "period_end",
        ),
        Index("ix_growth_reports_status", "status"),
        # F-V6-02：同 (student_id, period_label, period_start, period_end) 在
        # 非 failed 範圍內僅可有一筆，防 admin 連點 POST 雙建報告繞過 LINE
        # 推送 5 分鐘冪等（F-V6-01）。failed 允許重建以供 retry。
        Index(
            "uq_growth_reports_period_active",
            "student_id",
            "period_label",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=text("status != 'failed'"),
            sqlite_where=text("status != 'failed'"),
        ),
    )
