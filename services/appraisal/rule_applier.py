"""考核扣分規則套用引擎（Phase 1 calibrate）。

純函式設計，不接 DB；caller 負責從 DB 載 ScoringRule。

5 種 rule_type：
  PER_UNIT             count × per_unit_delta（支援 per-role override + caps）
  TIER                 依 input value 找 tier
  FLAT_THRESHOLD       單一閾值二分
  DISCIPLINARY_TIERED  REWARD_PUNISH 專用，warning/minor/major 各自單價
  MANUAL_DELTA         主任手填分值本身，依 min_delta/max_delta clamp
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AUTO_ITEM_CODES,
    MANUAL_ITEM_CODES,
    AppraisalManualEventCount,
    AppraisalScoringRule,
    RoleGroup,
)

logger = logging.getLogger(__name__)

_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class ScoringRule:
    item_code: str
    effective_from: date
    rule_type: str
    rule_config: dict
    applies_to_role_groups: Optional[list[str]]


def _round(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def apply_per_unit(rule: ScoringRule, count: Decimal, role_group: RoleGroup) -> Decimal:
    cfg = rule.rule_config
    # 1. 決定單價（per_role_override 優先）
    per_unit = Decimal(str(cfg["per_unit_delta"]))
    override = (cfg.get("per_role_override") or {}).get(role_group.value)
    if override is not None:
        per_unit = Decimal(str(override))
    # 2. unit_cap 限制
    effective_count = count
    cap = cfg.get("unit_cap")
    if cap is not None and count > cap:
        effective_count = Decimal(cap)
    # 3. 算 delta
    delta = per_unit * effective_count
    # 4. delta_cap 限制
    dcap = cfg.get("delta_cap")
    if dcap is not None:
        dcap_d = Decimal(str(dcap))
        if dcap_d < 0 and delta < dcap_d:
            delta = dcap_d
        elif dcap_d > 0 and delta > dcap_d:
            delta = dcap_d
    return _round(delta)


def apply_tier(rule: ScoringRule, value: Decimal, role_group: RoleGroup) -> Decimal:
    """value ≥ tier.min 的最大 tier 的 delta；tiers 內部自動依 min desc 排。"""
    tiers = sorted(rule.rule_config["tiers"], key=lambda t: t["min"], reverse=True)
    for tier in tiers:
        if value >= Decimal(str(tier["min"])):
            return _round(Decimal(str(tier["delta"])))
    logger.warning(
        "apply_tier: value=%s 沒 match 任何 tier（item=%s）",
        value,
        rule.item_code,
    )
    return Decimal("0")


def apply_flat_threshold(
    rule: ScoringRule,
    value: Decimal,
    role_group: RoleGroup,
    grade_name: Optional[str] = None,
) -> Decimal:
    """value >= threshold → above_delta；否則 below_delta。

    grade_name 非 None 時，先查 rule_config["grade_thresholds"][grade_name]；
    命中則用年級門檻覆蓋 threshold，否則回退既有 threshold（向後相容）。
    """
    cfg = rule.rule_config
    threshold = Decimal(str(cfg["threshold"]))
    if grade_name is not None:
        grade_thresholds = cfg.get("grade_thresholds") or {}
        if grade_name in grade_thresholds:
            threshold = Decimal(str(grade_thresholds[grade_name]))
    if value >= threshold:
        return _round(Decimal(str(cfg["above_delta"])))
    return _round(Decimal(str(cfg["below_delta"])))


def apply_disciplinary_tiered(
    rule: ScoringRule,
    warning_count: int,
    minor_count: int,
    major_count: int,
    *,
    commend_count: int = 0,
    minor_merit_count: int = 0,
    major_merit_count: int = 0,
) -> Decimal:
    """REWARD_PUNISH 專用：懲處（warning/minor/major）與獎勵（嘉獎/小功/大功）功過相抵。

    commend_delta/minor_merit_delta/major_merit_delta 用 cfg.get(..., 0) 向後相容，
    舊版 config 不含 merit 鍵時三者皆視為 0。
    """
    cfg = rule.rule_config
    delta = (
        Decimal(str(cfg["warning_delta"])) * Decimal(warning_count)
        + Decimal(str(cfg["minor_delta"])) * Decimal(minor_count)
        + Decimal(str(cfg["major_delta"])) * Decimal(major_count)
        + Decimal(str(cfg.get("commend_delta", 0))) * Decimal(commend_count)
        + Decimal(str(cfg.get("minor_merit_delta", 0))) * Decimal(minor_merit_count)
        + Decimal(str(cfg.get("major_merit_delta", 0))) * Decimal(major_merit_count)
    )
    return _round(delta)


def apply_manual_delta(
    rule: ScoringRule, value: Decimal, role_group: RoleGroup
) -> Decimal:
    """MANUAL_DELTA：count 欄存主任手填「分值」本身（可正可負）。

    依 rule_config 的 min_delta/max_delta clamp（API 層另有 422 驗證，
    此處 clamp 是第二道防線，保證舊資料/旁路寫入不會炸出範圍外分數）。
    """
    cfg = rule.rule_config
    lo = Decimal(str(cfg["min_delta"]))
    hi = Decimal(str(cfg["max_delta"]))
    v = Decimal(value)
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return _round(v)


# ===== DB-aware integration layer (Task 10) =====


@dataclass(frozen=True)
class DeltaResult:
    """compute_all_deltas 對單一 (participant, item_code) 的計算結果。"""

    delta: Decimal
    raw_value: Decimal
    note: str


def rule_applies_to_role(rule: ScoringRule, role_group: RoleGroup) -> bool:
    """applies_to_role_groups=None 視為全部；否則檢查 role_group.value 是否在 list 內。"""
    if rule.applies_to_role_groups is None:
        return True
    return role_group.value in rule.applies_to_role_groups


def load_rules_for_date(session: Session, on_date: date) -> dict[str, ScoringRule]:
    """每個 item_code 取 effective_from ≤ on_date 的最新版。

    回傳 dict[item_code] = ScoringRule（純 dataclass，與 DB 解耦）。
    沒有任何生效版本的 item_code 不會出現在 dict 中。
    """
    stmt = (
        select(AppraisalScoringRule)
        .where(AppraisalScoringRule.effective_from <= on_date)
        .order_by(
            AppraisalScoringRule.item_code,
            AppraisalScoringRule.effective_from.desc(),
        )
    )
    rows = session.execute(stmt).scalars().all()
    out: dict[str, ScoringRule] = {}
    for row in rows:
        if row.item_code in out:
            continue
        out[row.item_code] = ScoringRule(
            item_code=row.item_code,
            effective_from=row.effective_from,
            rule_type=row.rule_type,
            rule_config=row.rule_config,
            applies_to_role_groups=row.applies_to_role_groups,
        )
    return out


def _coerce_role(raw) -> RoleGroup:
    """ParticipantStatus.role_group 在型別上是 str（aggregator 存 enum.value），
    但 fake_status / 部分 caller 可能直接傳 RoleGroup enum。統一轉成 enum。"""
    if isinstance(raw, RoleGroup):
        return raw
    return RoleGroup(raw)


def _apply_auto_item(
    rule: ScoringRule, status, role_group: RoleGroup
) -> tuple[Decimal, Decimal, str]:
    """5 auto item 從 ParticipantStatus 取值。

    （REWARD_PUNISH 也算 auto — 從 disciplinary aggregator 拿三類計數）
    """
    code = rule.item_code
    if code == "LATE_EARLY":
        cnt = Decimal(
            status.attendance.late_count + status.attendance.early_leave_count
        )
        delta = apply_per_unit(rule, cnt, role_group)
        return (
            delta,
            cnt,
            f"遲到 {status.attendance.late_count} / 早退 {status.attendance.early_leave_count}",
        )
    if code == "MISSING_PUNCH":
        cnt = Decimal(status.attendance.missing_punch_count)
        return apply_per_unit(rule, cnt, role_group), cnt, f"未打卡 {cnt} 次"
    if code == "LEAVE":
        cnt = Decimal(status.attendance.leave_days)
        return apply_per_unit(rule, cnt, role_group), cnt, f"請假 {cnt} 天"
    if code in ("RETURNING_RATE_0915", "RETURNING_RATE_0315"):
        rate = (
            status.retention.retention_rate
            if status.retention.retention_rate is not None
            else Decimal("0")
        )
        return apply_tier(rule, rate, role_group), rate, f"留校率 {rate}%"
    if code == "AFTER_CLASS_RATE":
        rate = (
            status.activity.activity_rate
            if status.activity.activity_rate is not None
            else Decimal("0")
        )
        return (
            apply_flat_threshold(
                rule, rate, role_group, grade_name=status.activity.grade_name
            ),
            rate,
            f"才藝率 {rate}%",
        )
    if code == "REWARD_PUNISH":
        d = status.disciplinary
        delta = apply_disciplinary_tiered(
            rule,
            d.warning_count,
            d.minor_count,
            d.major_count,
            commend_count=d.commend_count,
            minor_merit_count=d.minor_merit_count,
            major_merit_count=d.major_merit_count,
        )
        raw = Decimal(
            d.warning_count
            + d.minor_count
            + d.major_count
            + d.commend_count
            + d.minor_merit_count
            + d.major_merit_count
        )
        return (
            delta,
            raw,
            f"警告 {d.warning_count} / 小過 {d.minor_count} / 大過 {d.major_count}"
            f" / 嘉獎 {d.commend_count} / 小功 {d.minor_merit_count}"
            f" / 大功 {d.major_merit_count}",
        )
    if code == "CLASS_HEADCOUNT_BONUS":
        cnt = Decimal(status.headcount_over_target)
        return (
            apply_per_unit(rule, cnt, role_group),
            cnt,
            f"超編制 {status.headcount_over_target} 人",
        )
    raise ValueError(f"_apply_auto_item: 未知 auto item_code {code}")


def compute_all_deltas(session: Session, cycle) -> dict[tuple[int, str], DeltaResult]:
    """對 cycle 內所有 participant 的 14 個 item_code 算 delta。

    流程：
      1. load_rules_for_date(cycle.base_score_calc_date)
      2. aggregate_cycle_status(session, cycle) 取 5 auto 原始值
      3. 批撈 AppraisalManualEventCount 取 9 manual 手填次數
      4. 對每 (participant, item_code) 依 auto/manual 分流計算
      5. role 不適用者寫入 delta=0、note="本角色不適用"
    """
    from services.appraisal.status_aggregator import aggregate_cycle_status

    rules = load_rules_for_date(session, cycle.base_score_calc_date)
    statuses = aggregate_cycle_status(session, cycle)

    # 批撈 manual counts
    manual_rows = (
        session.execute(
            select(AppraisalManualEventCount).where(
                AppraisalManualEventCount.cycle_id == cycle.id
            )
        )
        .scalars()
        .all()
    )
    counts_by_pid_code: dict[tuple[int, str], Decimal] = {
        (r.participant_id, r.item_code): r.count for r in manual_rows
    }

    result: dict[tuple[int, str], DeltaResult] = {}
    auto_codes = {c.value for c in AUTO_ITEM_CODES}
    manual_codes = {c.value for c in MANUAL_ITEM_CODES}

    # bug sweep 2026-05-18 P2：DB 漏掉某 expected code（migration 失效、admin 誤刪、
    # effective_from 設未來日尚未生效）會讓該 code 對所有 participant 不產出 delta，
    # 被靜默當作 0，UI 也不會閃任何警示。改為在迴圈前明確 log warning，
    # 讓 SRE 從 logs 即可發現「規則資料完整性出問題」。
    expected_codes = auto_codes | manual_codes
    missing_codes = expected_codes - rules.keys()
    for code in sorted(missing_codes):
        logger.warning(
            "compute_all_deltas: 找不到 item_code=%s 的有效 rule（cycle_id=%s, "
            "effective_on=%s）— 該 code 將不產出 delta",
            code,
            cycle.id,
            cycle.base_score_calc_date,
        )

    for status in statuses:
        role = _coerce_role(status.role_group)
        for code, rule in rules.items():
            if not rule_applies_to_role(rule, role):
                result[(status.participant_id, code)] = DeltaResult(
                    Decimal("0"), Decimal("0"), "本角色不適用"
                )
                continue
            if code in auto_codes:
                delta, raw, note = _apply_auto_item(rule, status, role)
                result[(status.participant_id, code)] = DeltaResult(delta, raw, note)
            elif code in manual_codes:
                cnt = counts_by_pid_code.get(
                    (status.participant_id, code), Decimal("0")
                )
                if rule.rule_type == "PER_UNIT":
                    delta = apply_per_unit(rule, cnt, role)
                elif rule.rule_type == "TIER":
                    delta = apply_tier(rule, cnt, role)
                elif rule.rule_type == "FLAT_THRESHOLD":
                    delta = apply_flat_threshold(rule, cnt, role)
                else:
                    delta = Decimal("0")
                result[(status.participant_id, code)] = DeltaResult(
                    delta, cnt, f"手填 {cnt} 次" if cnt else "未填"
                )
            else:
                logger.warning(
                    "compute_all_deltas: rule item_code=%s 不在 auto/manual 集合內，略過",
                    code,
                )
    return result


__all__ = [
    "DeltaResult",
    "ScoringRule",
    "apply_disciplinary_tiered",
    "apply_flat_threshold",
    "apply_manual_delta",
    "apply_per_unit",
    "apply_tier",
    "compute_all_deltas",
    "load_rules_for_date",
    "rule_applies_to_role",
]
