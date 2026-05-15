"""Target: services/leave_policy.py 的純函式。

被攻擊對象:
- requires_supporting_document(start, end) -> bool
- validate_portal_leave_rules(leave_type, start, end, hours, *, today) -> None | ValueError

不變量(invariants):
- IV1 calendar_days_consistency:
    requires_supporting_document(s, e) == True ⟺ (e - s).days + 1 > 2
- IV2 personal_advance_notice:
    leave_type="personal" 且 start < today + 2 → 必須 raise ValueError
- IV3 personal_late_ok:
    leave_type="personal" 且 start >= today + 2 → 不應 raise
- IV4 sick_increment:
    leave_type="sick" 且 hours % 4 != 0 → 必須 raise ValueError
- IV5 sick_multiple_ok:
    leave_type="sick" 且 hours % 4 == 0 → 不應 raise
- IV6 end_before_start:
    (對 requires_supporting_document) end < start 應不會 crash;結果保留為 bool 即可
"""

from __future__ import annotations

from datetime import date, timedelta

from services.leave_policy import (
    requires_supporting_document,
    validate_portal_leave_rules,
)

from evals.core.target import Invariant, Target

FIXED_TODAY = date(2026, 5, 14)  # 固定時間鎖,避免測試依賴 wall clock


def _runner(case: dict) -> dict:
    """執行兩個函式並回傳一個 result dict;invariant 各自挑感興趣的鍵看。"""
    leave_type = case.get("leave_type", "personal")
    start = case.get("start_date")
    end = case.get("end_date")
    hours = case.get("leave_hours", 0)
    today = case.get("today", FIXED_TODAY)

    out: dict = {}
    # requires_supporting_document 只在 start/end 都是 date 時有意義
    try:
        out["requires_doc"] = requires_supporting_document(start, end)
    except Exception as exc:  # noqa: BLE001
        out["requires_doc_exc"] = f"{type(exc).__name__}: {exc}"

    # validate_portal_leave_rules:沒 raise 視為 ok=True
    try:
        validate_portal_leave_rules(leave_type, start, end, hours, today=today)
        out["validate_ok"] = True
    except ValueError as exc:
        out["validate_ok"] = False
        out["validate_error"] = str(exc)
    return out


def _iv1(case: dict, outcome: dict):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if "requires_doc" not in res:
        return None
    start = case.get("start_date")
    end = case.get("end_date")
    if not (isinstance(start, date) and isinstance(end, date)):
        return None
    expected = (end - start).days + 1 > 2
    if res["requires_doc"] != expected:
        return (
            f"requires_supporting_document returned {res['requires_doc']} "
            f"but expected {expected} for ({start}..{end}) span {(end - start).days + 1}d"
        )
    return None


def _iv2(case: dict, outcome: dict):
    if case.get("leave_type") != "personal":
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    start = case.get("start_date")
    today = case.get("today", FIXED_TODAY)
    if not isinstance(start, date) or not isinstance(today, date):
        return None
    earliest = today + timedelta(days=2)
    if start < earliest:
        if res.get("validate_ok"):
            return f"事假 start={start} < today+2={earliest} 但 validate 通過"
    return None


def _iv3(case: dict, outcome: dict):
    if case.get("leave_type") != "personal":
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    start = case.get("start_date")
    end = case.get("end_date")
    today = case.get("today", FIXED_TODAY)
    hours = case.get("leave_hours", 0)
    if not all(isinstance(x, date) for x in (start, end, today)):
        return None
    # 排除 hours <= 0 的 case;IV6 已負責這條防線
    if isinstance(hours, (int, float)) and hours <= 0:
        return None
    if start >= today + timedelta(days=2):
        if not res.get("validate_ok"):
            err = res.get("validate_error", "?")
            return f"事假 start={start} 已滿足提前 2 日但被擋: {err}"
    return None


def _iv4(case: dict, outcome: dict):
    if case.get("leave_type") != "sick":
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    hours = case.get("leave_hours", 0)
    if not isinstance(hours, (int, float)):
        return None
    if hours % 4 != 0:
        if res.get("validate_ok"):
            return f"病假 hours={hours} 非 4 倍數但 validate 通過"
    return None


def _iv5(case: dict, outcome: dict):
    if case.get("leave_type") != "sick":
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    hours = case.get("leave_hours", 0)
    if not isinstance(hours, (int, float)):
        return None
    if hours > 0 and hours % 4 == 0:
        if not res.get("validate_ok"):
            err = res.get("validate_error", "?")
            return f"病假 hours={hours} 為 4 倍數但被擋: {err}"
    return None


def _iv6(case: dict, outcome: dict):
    """病假時數應 > 0;Python `-4 % 4 == 0` 會讓負時數通過 IV4 檢查,
    但業務上不存在負數時數請假。"""
    if case.get("leave_type") != "sick":
        return None
    if not outcome.get("ok"):
        return None
    hours = case.get("leave_hours", 0)
    if not isinstance(hours, (int, float)):
        return None
    if hours <= 0:
        if outcome["result"].get("validate_ok"):
            return f"病假 hours={hours} (≤0) 通過 validate;規則沒檢查時數正向性"
    return None


TARGET = Target(
    name="leave_policy",
    description=(
        "請假規則純邏輯 helper: requires_supporting_document(超過 2 曆日須附證明)"
        "與 validate_portal_leave_rules(事假提前 2 日 / 病假 4h 單位)。"
    ),
    signature={
        "fields": {
            "leave_type": {"type": "string", "enum": ["personal", "sick", "annual"]},
            "start_date": {"type": "date"},
            "end_date": {"type": "date"},
            "leave_hours": {
                "type": "float",
                "boundary": [0, 1, 4, 8, 16, 0.5, 3.999, 4.001],
            },
            "today": {"type": "date"},
        },
        "notes": (
            "personal 需 start >= today+2;sick 需 hours%4==0;"
            "兩種情況違反皆 raise ValueError。"
        ),
    },
    invariants=[
        Invariant("IV1_calendar_days", "requires_doc ⟺ (e-s).days+1 > 2", _iv1),
        Invariant(
            "IV2_personal_advance_blocks", "personal 提前不足 2 日 → raise", _iv2
        ),
        Invariant("IV3_personal_advance_ok", "personal 提前 ≥ 2 日 → 不 raise", _iv3),
        Invariant("IV4_sick_non_multiple_blocks", "sick hours 非 4 倍 → raise", _iv4),
        Invariant("IV5_sick_multiple_ok", "sick hours 4 倍 → 不 raise", _iv5),
        Invariant("IV6_sick_positive_hours", "sick hours 必須 > 0", _iv6),
    ],
    seed_cases=[
        {
            "leave_type": "personal",
            "start_date": date(2026, 5, 20),
            "end_date": date(2026, 5, 20),
            "leave_hours": 8,
            "today": FIXED_TODAY,
        },
        {
            "leave_type": "sick",
            "start_date": date(2026, 5, 14),
            "end_date": date(2026, 5, 14),
            "leave_hours": 4,
            "today": FIXED_TODAY,
        },
        {
            "leave_type": "personal",
            "start_date": date(2026, 5, 16),
            "end_date": date(2026, 5, 18),
            "leave_hours": 24,
            "today": FIXED_TODAY,
        },
    ],
    runner=_runner,
    allowed_exceptions=(),  # ValueError 已在 runner 內捕,所以 runner 不會丟
)
