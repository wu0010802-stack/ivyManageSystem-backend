"""考核扣分規則套用引擎（Phase 1 calibrate）。

純函式設計，不接 DB；caller 負責從 DB 載 ScoringRule。

4 種 rule_type：
  PER_UNIT             count × per_unit_delta（支援 per-role override + caps）
  TIER                 依 input value 找 tier
  FLAT_THRESHOLD       單一閾值二分
  DISCIPLINARY_TIERED  REWARD_PUNISH 專用，warning/minor/major 各自單價
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from models.appraisal import RoleGroup

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
    rule: ScoringRule, value: Decimal, role_group: RoleGroup
) -> Decimal:
    """value >= threshold → above_delta；否則 below_delta。"""
    cfg = rule.rule_config
    threshold = Decimal(str(cfg["threshold"]))
    if value >= threshold:
        return _round(Decimal(str(cfg["above_delta"])))
    return _round(Decimal(str(cfg["below_delta"])))


def apply_disciplinary_tiered(
    rule: ScoringRule,
    warning_count: int,
    minor_count: int,
    major_count: int,
) -> Decimal:
    """REWARD_PUNISH 專用：warning/minor/major 各自單價乘以件數後加總。"""
    cfg = rule.rule_config
    delta = (
        Decimal(str(cfg["warning_delta"])) * Decimal(warning_count)
        + Decimal(str(cfg["minor_delta"])) * Decimal(minor_count)
        + Decimal(str(cfg["major_delta"])) * Decimal(major_count)
    )
    return _round(delta)
