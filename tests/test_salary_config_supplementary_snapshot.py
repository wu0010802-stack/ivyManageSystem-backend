"""Regression: config_for_month snapshot 必須涵蓋二代健保補充保費費率。

_snapshot_config_state 的 insurance dict 原本漏了 supplementary_health_rate /
supplementary_health_threshold。後果：config_for_month 重算歷史月時，
_apply_configs_for_month 會把該月費率寫進 insurance_service singleton，但離開
context 時 _restore_config_state 用 `for k,v in snapshot["insurance"].items()`
只還原 snapshot 內有的 key → 這兩個費率永遠不被還原，洩漏到後續計算。
"""

from services.salary.engine import SalaryEngine


def test_snapshot_restore_roundtrips_supplementary_health_fields():
    engine = SalaryEngine(load_from_db=False)
    engine.insurance_service.supplementary_health_rate = 0.0211
    engine.insurance_service.supplementary_health_threshold = 29500

    snap = engine._snapshot_config_state()

    # 模擬 _apply_configs_for_month 在 config_for_month 內寫入「該月」不同費率
    engine.insurance_service.supplementary_health_rate = 0.99
    engine.insurance_service.supplementary_health_threshold = 88888

    engine._restore_config_state(snap)

    # 修復前：snapshot 漏這兩 key → restore 還原不了 → 仍是 0.99 / 88888（洩漏）
    assert engine.insurance_service.supplementary_health_rate == 0.0211
    assert engine.insurance_service.supplementary_health_threshold == 29500
