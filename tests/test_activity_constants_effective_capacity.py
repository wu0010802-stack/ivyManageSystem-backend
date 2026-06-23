"""effective_capacity / OCCUPYING_STATUSES 單一來源契約測試。

口徑收斂（2026-06-23）：把散落各處的 `capacity if not None else 30` 與
`["enrolled", "promoted_pending"]` 收斂到 utils.activity_constants 單一來源。
本測試鎖定該 helper 的關鍵語意（尤其 0 與 None 不同），避免日後漂移。
"""

from types import SimpleNamespace

from utils.activity_constants import (
    OCCUPYING_STATUSES,
    DEFAULT_COURSE_CAPACITY,
    effective_capacity,
)


def _course(capacity):
    return SimpleNamespace(capacity=capacity)


def test_null_capacity_falls_back_to_default():
    assert effective_capacity(_course(None)) == DEFAULT_COURSE_CAPACITY == 30


def test_zero_capacity_is_preserved_not_treated_as_null():
    # 0 與 None 語意不同：明確 0 表示不開放名額，須原樣保留（不可 fallback 成 30）。
    assert effective_capacity(_course(0)) == 0


def test_positive_capacity_passthrough():
    assert effective_capacity(_course(12)) == 12


def test_occupying_statuses_contains_both_occupying_states():
    # 漏掉 promoted_pending 會超發候補，這條集合是容量閘的唯一真相來源。
    assert set(OCCUPYING_STATUSES) == {"enrolled", "promoted_pending"}


def test_service_layer_reexports_same_objects():
    # 既有 `from services.activity_service import OCCUPYING_STATUSES / DEFAULT_COURSE_CAPACITY`
    # 仍須可用且指向同一份（向後相容）。
    import services.activity_service as svc

    assert svc.OCCUPYING_STATUSES is OCCUPYING_STATUSES
    assert svc.DEFAULT_COURSE_CAPACITY == DEFAULT_COURSE_CAPACITY
