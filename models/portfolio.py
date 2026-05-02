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

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)

from models.base import Base

# ── Attachment owner_type 枚舉 ────────────────────────────────────────────
ATTACHMENT_OWNER_OBSERVATION = "observation"
ATTACHMENT_OWNER_REPORT = "report"
ATTACHMENT_OWNER_MEDICATION_ORDER = "medication_order"
ATTACHMENT_OWNER_MESSAGE = "message"
ATTACHMENT_OWNER_EVENT_ACK = "event_acknowledgment"
ATTACHMENT_OWNER_STUDENT_LEAVE = "student_leave"
ATTACHMENT_OWNER_CONTACT_BOOK = "contact_book_entry"
ATTACHMENT_OWNER_TYPES = (
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_REPORT,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_MESSAGE,
    ATTACHMENT_OWNER_EVENT_ACK,
    ATTACHMENT_OWNER_STUDENT_LEAVE,
    ATTACHMENT_OWNER_CONTACT_BOOK,
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
    created_at = Column(DateTime, default=datetime.now, nullable=False)

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

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
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
    allergen = Column(
        String(100), nullable=False, comment="過敏原，如 花生 / 乳製品 / 塵蟎"
    )
    severity = Column(
        String(10),
        nullable=False,
        comment="mild / moderate / severe",
    )
    reaction_symptom = Column(String(200), nullable=True, comment="過敏反應症狀")
    first_aid_note = Column(Text, nullable=True, comment="急救處置說明")
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
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

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
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

    created_at = Column(DateTime, default=datetime.now, nullable=False)

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
