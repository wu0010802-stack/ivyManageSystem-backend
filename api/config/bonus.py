"""api/config/bonus.py — 獎金設定 (get/put/history)。

3 個 endpoint + 1 個 Pydantic schema：
- GET  /bonus              取得目前 active 獎金設定（5 分鐘 TTL cache）
- PUT  /bonus              建立新版本（複製舊欄位 → 套用變更 → mark stale）
- GET  /bonus/history      所有歷史版本

依賴 __init__ 的 _clear_cache / _CACHE_TTL_CONFIG /
_mark_existing_salary_stale_for_config / _salary_engine 經 lazy back-import 取得
（同 .line / .position_salary pattern）；cache 本身改走 utils.cache_layer.get_cache()
namespace = "config_bonus"。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_

from models.database import (
    get_session,
    BonusConfig as DBBonusConfig,
    GradeTarget,
)
from utils.auth import require_staff_permission
from utils.cache_layer import get_cache
from utils.constants import MIN_CONFIG_YEAR, MAX_CONFIG_YEAR
from utils.errors import raise_safe_500
from utils.finance_guards import has_finance_approve, require_adjustment_reason
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter()

# BonusConfig 所有可複製的業務欄位（不含 id/version/changed_by/is_active/timestamps）
_BONUS_FIELDS = [
    "config_year",
    "head_teacher_ab",
    "head_teacher_c",
    "assistant_teacher_ab",
    "assistant_teacher_c",
    "principal_festival",
    "director_festival",
    "leader_festival",
    "driver_festival",
    "designer_festival",
    "admin_festival",
    "principal_dividend",
    "director_dividend",
    "leader_dividend",
    "vice_leader_dividend",
    "overtime_head_normal",
    "overtime_head_baby",
    "overtime_assistant_normal",
    "overtime_assistant_baby",
    "school_wide_target",
    # 階段 2-B（2026-05-07）：園規常數從 hardcode 搬到 BonusConfig
    "meeting_default_hours",
    "meeting_absence_penalty",
    "art_teacher_festival",
]


class BonusConfigUpdate(BaseModel):
    """獎金設定更新"""

    config_year: Optional[int] = Field(None, ge=MIN_CONFIG_YEAR, le=MAX_CONFIG_YEAR)
    head_teacher_ab: Optional[float] = Field(None, ge=0)
    head_teacher_c: Optional[float] = Field(None, ge=0)
    assistant_teacher_ab: Optional[float] = Field(None, ge=0)
    assistant_teacher_c: Optional[float] = Field(None, ge=0)
    principal_festival: Optional[float] = Field(None, ge=0)
    director_festival: Optional[float] = Field(None, ge=0)
    leader_festival: Optional[float] = Field(None, ge=0)
    driver_festival: Optional[float] = Field(None, ge=0)
    designer_festival: Optional[float] = Field(None, ge=0)
    admin_festival: Optional[float] = Field(None, ge=0)
    principal_dividend: Optional[float] = Field(None, ge=0)
    director_dividend: Optional[float] = Field(None, ge=0)
    leader_dividend: Optional[float] = Field(None, ge=0)
    vice_leader_dividend: Optional[float] = Field(None, ge=0)
    overtime_head_normal: Optional[float] = Field(None, ge=0)
    overtime_head_baby: Optional[float] = Field(None, ge=0)
    overtime_assistant_normal: Optional[float] = Field(None, ge=0)
    overtime_assistant_baby: Optional[float] = Field(None, ge=0)
    school_wide_target: Optional[int] = Field(None, ge=0)
    # 階段 2-B（2026-05-07）：園規常數
    # 上限：每場會議計薪不超過勞基法每日加班上限（防誤輸 99）
    meeting_default_hours: Optional[float] = Field(None, ge=0, le=12)
    # 上限：缺席扣節慶獎金不應超過獎金本身常見上限（防扣到負值）
    meeting_absence_penalty: Optional[int] = Field(None, ge=0, le=10000)
    art_teacher_festival: Optional[float] = Field(None, ge=0)
    # 金流硬化（2026-05-16 P1-5）：BonusConfig 變動影響全員獎金基數，
    # 與 insurance.brackets PUT 對齊要求金流簽核 + 異動原因 ≥10 字。
    # 前端必須附 reason 才能呼叫此端點。
    reason: Optional[str] = Field(
        default=None, description="變更原因（必填，至少 10 個字落入 audit）"
    )


@router.get("/bonus")
def get_bonus_config(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得獎金設定"""
    cached = get_cache().get("config_bonus", "v")
    if cached is not None:
        return cached

    session = get_session()
    try:
        config = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.config_year.desc(), DBBonusConfig.id.desc())
            .first()
        )
        if not config:
            return {}
        result = {
            "id": config.id,
            "config_year": config.config_year,
            "head_teacher_ab": config.head_teacher_ab,
            "head_teacher_c": config.head_teacher_c,
            "assistant_teacher_ab": config.assistant_teacher_ab,
            "assistant_teacher_c": config.assistant_teacher_c,
            "principal_festival": config.principal_festival,
            "director_festival": config.director_festival,
            "leader_festival": config.leader_festival,
            "driver_festival": config.driver_festival,
            "designer_festival": config.designer_festival,
            "admin_festival": config.admin_festival,
            "principal_dividend": config.principal_dividend,
            "director_dividend": config.director_dividend,
            "leader_dividend": config.leader_dividend,
            "vice_leader_dividend": config.vice_leader_dividend,
            "overtime_head_normal": config.overtime_head_normal,
            "overtime_head_baby": config.overtime_head_baby,
            "overtime_assistant_normal": config.overtime_assistant_normal,
            "overtime_assistant_baby": config.overtime_assistant_baby,
            "school_wide_target": config.school_wide_target,
        }
        from . import _CACHE_TTL_CONFIG  # lazy back-import

        get_cache().set("config_bonus", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


@router.put("/bonus")
def update_bonus_config(
    data: BonusConfigUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新獎金設定（建立新版本，保留舊版歷程，同步複製年級目標）。

    金流硬化（bug sweep 2026-05-16 P1-5）：與 PUT /insurance/brackets 對齊
    - 只有 SETTINGS_WRITE 不夠（HR 行政都有）→ 額外要求 has_finance_approve
      （ACTIVITY_PAYMENT_APPROVE），否則 admin 可繞過 manual_adjust 500K 上限+簽核。
    - reason 必填 ≥10 字，會與 changed_fields 一併寫入 audit_logs.changes。
    """
    from . import (  # lazy back-import
        _clear_cache,
        _mark_existing_salary_stale_for_config,
        _salary_engine,
    )

    if not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "獎金設定變更影響全員獎金基數，需由具備『金流簽核』權限者"
                "（ACTIVITY_PAYMENT_APPROVE）執行"
            ),
        )
    cleaned_reason = require_adjustment_reason(data.reason)

    session = get_session()
    try:
        old_config = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.id.desc())
            .first()
        )

        # 複製舊版欄位值，再套用本次變更
        new_config = DBBonusConfig(is_active=True)
        if old_config:
            for field in _BONUS_FIELDS:
                setattr(new_config, field, getattr(old_config, field, None))
            new_config.version = (old_config.version or 1) + 1
        else:
            new_config.version = 1
            new_config.config_year = 2026

        new_config.changed_by = current_user.get("username")

        update_data = data.model_dump(exclude_unset=True)
        # reason 不是 BonusConfig 表的欄位，落到 audit_changes 即可
        update_data.pop("reason", None)
        changed_diff: dict[str, dict[str, object]] = {}
        for key, value in update_data.items():
            if value is None:
                continue
            old_value = getattr(old_config, key, None) if old_config else None
            setattr(new_config, key, value)
            if old_value != value:
                changed_diff[key] = {"old": old_value, "new": value}

        if old_config:
            old_config.is_active = False

        session.add(new_config)
        session.flush()  # 取得 new_config.id

        # 複製年級目標到新版本：合併 NULL（舊資料）與版本特定目標
        # 策略：NULL 目標作為基礎，舊版本目標覆蓋同年級的 NULL 值
        # 這樣即使只有部分年級已綁定到舊版本 ID（其他仍為 NULL），所有年級都能被複製
        conds = [GradeTarget.bonus_config_id == None]  # noqa: E711
        if old_config:
            conds.append(GradeTarget.bonus_config_id == old_config.id)
        all_grade_targets = session.query(GradeTarget).filter(or_(*conds)).all()
        # 合併：版本目標（bonus_config_id is not None）優先覆蓋 NULL 目標
        null_targets = {
            gt.grade_name: gt for gt in all_grade_targets if gt.bonus_config_id is None
        }
        versioned_targets = {
            gt.grade_name: gt
            for gt in all_grade_targets
            if gt.bonus_config_id is not None
        }
        merged_targets = {**null_targets, **versioned_targets}

        for grade_name, gt in merged_targets.items():
            session.add(
                GradeTarget(
                    config_year=gt.config_year,
                    grade_name=grade_name,
                    festival_two_teachers=gt.festival_two_teachers,
                    festival_one_teacher=gt.festival_one_teacher,
                    festival_shared=gt.festival_shared,
                    overtime_two_teachers=gt.overtime_two_teachers,
                    overtime_one_teacher=gt.overtime_one_teacher,
                    overtime_shared=gt.overtime_shared,
                    bonus_config_id=new_config.id,
                )
            )

        stale_marked = _mark_existing_salary_stale_for_config(
            session, bonus_config_id=new_config.id
        )
        session.commit()
        if stale_marked:
            logger.warning(
                "獎金設定更新後標記 %d 筆未封存薪資為 needs_recalc(舊獎金 id=%s → 新 id=%s)",
                stale_marked,
                old_config.id if old_config else None,
                new_config.id,
            )
        if _salary_engine is not None:
            _salary_engine.load_config_from_db()
        _clear_cache("bonus")

        # AuditMiddleware 會把 entity_type=config / action=UPDATE 寫入 audit_logs。
        # changes 帶 reason + 改動欄位 old/new + 觸發 stale 數，事後可在
        # audit-logs 篩 entity_type=config 看到完整異動軌跡。
        request.state.audit_changes = {
            "reason": cleaned_reason,
            "changed_fields": changed_diff,
            "old_version": old_config.version if old_config else None,
            "new_version": new_config.version,
            "salary_records_marked_stale": stale_marked,
        }
        request.state.audit_entity_id = str(new_config.id)
        request.state.audit_summary = (
            f"獎金設定改版 v{new_config.version}"
            f"（{len(changed_diff)} 欄變動；{cleaned_reason[:30]}）"
        )

        return {
            "message": "獎金設定更新成功",
            "version": new_config.version,
            "id": new_config.id,
            "salary_records_marked_stale": stale_marked,
        }
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/bonus/history")
def get_bonus_config_history(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得獎金設定所有歷史版本（最新在前）"""
    session = get_session()
    try:
        configs = (
            session.query(DBBonusConfig).order_by(DBBonusConfig.created_at.desc()).all()
        )
        return [
            {
                "id": c.id,
                "version": c.version,
                "config_year": c.config_year,
                "is_active": c.is_active,
                "changed_by": c.changed_by,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "head_teacher_ab": c.head_teacher_ab,
                "head_teacher_c": c.head_teacher_c,
                "assistant_teacher_ab": c.assistant_teacher_ab,
                "assistant_teacher_c": c.assistant_teacher_c,
                "school_wide_target": c.school_wide_target,
            }
            for c in configs
        ]
    finally:
        session.close()
