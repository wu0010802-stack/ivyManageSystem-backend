"""provenance registry：key → DerivedValue 的泛型分派。

新增 provider 時：在對應 provider 模組產 {key->DerivedValue} 的函式，
於 _GROUP_KEYS 註冊其『群組函式』與其 key 即可（一次算同模組多 key，避免重複查）。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from models.employee import Employee
from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue
from services.provenance.attendance_provider import derive_attendance_provenance

# 群組函式型別：(db, cycle, emp) -> {key: DerivedValue}
ProviderGroup = Callable[[Session, YearEndCycle, Employee], dict[str, DerivedValue]]

# 群組函式 → 其產出的 key（靜態，免 db 即可建索引）
_GROUP_KEYS: dict[ProviderGroup, tuple[str, ...]] = {
    derive_attendance_provenance: (
        "attendance_late",
        "personal_leave",
        "sick_leave",
        "meeting_absence",
    ),
}

# key → 群組函式（import 時建好；重複 key 立即炸，守護擴充路徑）
_KEY_TO_GROUP: dict[str, ProviderGroup] = {}
for _fn, _keys in _GROUP_KEYS.items():
    for _k in _keys:
        assert _k not in _KEY_TO_GROUP, f"duplicate provenance key: {_k!r}"
        _KEY_TO_GROUP[_k] = _fn
del _fn, _k, _keys

KNOWN_KEYS: frozenset[str] = frozenset(_KEY_TO_GROUP)


def resolve_provenance(
    db: Session, cycle: YearEndCycle, emp: Employee, key: str
) -> DerivedValue:
    """依 key 分派到對應 provider 群組，回傳該 key 的 DerivedValue。

    群組函式一次算同模組所有 key，這裡只取要的那個。未知 key → KeyError。"""
    if key not in _KEY_TO_GROUP:
        raise KeyError(f"unknown provenance key: {key}")
    return _KEY_TO_GROUP[key](db, cycle, emp)[key]
