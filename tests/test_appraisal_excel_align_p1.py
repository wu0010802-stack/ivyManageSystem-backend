"""P1 對齊 Excel：bonus 率對齊 + effective-date 覆蓋 114學年上。

Step 1 純函式 spec-lock（驗 engine 預期值）
Step 2 migration 常數-lock（驗 ALIGNED_RATES / RULES 模組常數；不跑原生 upgrade()
       以避免 CAST(:x AS JSONB) 在 SQLite in-memory 炸掉）。
"""

from datetime import date
from decimal import Decimal

import importlib.util
import pathlib

from services.appraisal.engine import BonusRateLookup, compute_bonus_amount
from models.appraisal import RoleGroup, Grade

# ─── 對齊後預期值（Excel 3 組）─────────────────────────────────────────────

ALIGNED = {
    (RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("8000"),
    (RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
    (RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
    (RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("5500"),
    (RoleGroup.ASSISTANT, Grade.GOOD): Decimal("3500"),
    (RoleGroup.STAFF, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.STAFF, Grade.GOOD): Decimal("4000"),
    (RoleGroup.COOK, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.COOK, Grade.GOOD): Decimal("4000"),
}


def _lookup(effective="2025-08-01"):
    return BonusRateLookup(
        rates={(effective, rg, gr): amt for (rg, gr), amt in ALIGNED.items()}
    )


# ─── Step 1: 純函式 spec-lock ──────────────────────────────────────────────


def test_assistant_outstanding_aligned_5500():
    """助理優等 100 分 → 5500（對齊 Excel，原 4500）。"""
    b = compute_bonus_amount(
        Decimal("100"),
        Grade.OUTSTANDING,
        RoleGroup.ASSISTANT,
        _lookup(),
        date(2025, 9, 15),
    )
    assert b == Decimal("5500.00")


def test_cook_good_aligned_4000():
    """廚工甲等 100 分 → 4000（對齊 Excel，原 2500）。"""
    b = compute_bonus_amount(
        Decimal("100"),
        Grade.GOOD,
        RoleGroup.COOK,
        _lookup(),
        date(2025, 9, 15),
    )
    assert b == Decimal("4000.00")


def test_114_cycle_date_resolves_rate_not_silent_zero():
    """114上 base date 2025-09-15 必須查得到 rate（effective_from 2025-08-01 ≤ 該日）。

    這是 silent-0 的核心驗證：若 effective_from 只有 2026-08-01，
    resolve() 查不到 ≤ 2025-09-15 的行，回傳 None → bonus=0（silent bug）。
    """
    lk = _lookup("2025-08-01")
    assert lk.resolve(date(2025, 9, 15), RoleGroup.HEAD_TEACHER, Grade.GOOD) == Decimal(
        "4000"
    )


def test_staff_outstanding_aligned_6000():
    """行政職優等對齊 6000（原 5000）。"""
    b = compute_bonus_amount(
        Decimal("100"),
        Grade.OUTSTANDING,
        RoleGroup.STAFF,
        _lookup(),
        date(2025, 9, 15),
    )
    assert b == Decimal("6000.00")


def test_supervisor_rates_unchanged():
    """督導群獎金不變（8000/5000），確認 SUPERVISOR 未受影響。"""
    lk = _lookup("2025-08-01")
    assert lk.resolve(
        date(2025, 9, 15), RoleGroup.SUPERVISOR, Grade.OUTSTANDING
    ) == Decimal("8000")
    assert lk.resolve(date(2025, 9, 15), RoleGroup.SUPERVISOR, Grade.GOOD) == Decimal(
        "5000"
    )


# ─── Step 2: migration 常數-lock ──────────────────────────────────────────


def _load_migration_module():
    """importlib 載入 migration 模組（不執行 upgrade()）。"""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    candidates = sorted(repo_root.glob("alembic/versions/*apxlal01*.py"))
    assert candidates, "找不到 *apxlal01* migration 檔"
    spec = importlib.util.spec_from_file_location("apxlal01", candidates[0])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_aligned_rates_assistant_outstanding_5500():
    """migration 常數：ASSISTANT/OUTSTANDING base_amount == 5500。"""
    mod = _load_migration_module()
    rate_map = {(rg, gr): amt for rg, gr, amt in mod.ALIGNED_RATES}
    assert rate_map[("ASSISTANT", "OUTSTANDING")] == 5500


def test_migration_aligned_rates_staff_good_4000():
    """migration 常數：STAFF/GOOD base_amount == 4000。"""
    mod = _load_migration_module()
    rate_map = {(rg, gr): amt for rg, gr, amt in mod.ALIGNED_RATES}
    assert rate_map[("STAFF", "GOOD")] == 4000


def test_migration_aligned_rates_cook_outstanding_6000():
    """migration 常數：COOK/OUTSTANDING base_amount == 6000。"""
    mod = _load_migration_module()
    rate_map = {(rg, gr): amt for rg, gr, amt in mod.ALIGNED_RATES}
    assert rate_map[("COOK", "OUTSTANDING")] == 6000


def test_migration_rules_has_sped_per_unit_plus2():
    """migration 常數：RULES 含 SPED PER_UNIT per_unit_delta==2。"""
    mod = _load_migration_module()
    sped_rules = [(c, t, cfg, r) for c, t, cfg, r in mod.RULES if c == "SPED"]
    assert sped_rules, "SPED 不在 RULES 清單"
    code, rtype, cfg, _roles = sped_rules[0]
    assert rtype == "PER_UNIT"
    assert cfg["per_unit_delta"] == 2.0


def test_migration_rules_count_15():
    """migration 常數：RULES 共 15 條（14 原有 + SPED）。"""
    mod = _load_migration_module()
    assert len(mod.RULES) == 15


def test_migration_down_revision_is_acadterm01():
    """migration down_revision 接 acadterm01（確保單一鏈不分叉）。"""
    mod = _load_migration_module()
    assert mod.down_revision == "acadterm01"


def test_migration_effective_date_is_2025_08_01():
    """migration 的 ALIGN_EFFECTIVE 必須是 2025-08-01（114學年上起算）。"""
    mod = _load_migration_module()
    assert mod.ALIGN_EFFECTIVE == "2025-08-01"
