"""才藝報名狀態機（Enum + 合法轉移表 + transition 服務）。

第4波 階段 0（純新增，零行為變更）。spec：
docs/superpowers/specs/2026-06-23-activity-status-state-machine-design.md

對齊 services/student_lifecycle.py 慣例：
- 狀態以 str Enum 表示（值即現有字串 → wire 不變、column 仍 String(20)、不用 SQLEnum）
- 合法轉移表 ALLOWED_TRANSITIONS（純資料）+ is_*_transition_allowed 純函式
- transition_* 服務：validate → 更新 status → 寫 RegistrationChange 稽核
- 非法轉移 raise ActivityTransitionError（ValueError 子類）→ 既有 handler 轉 HTTP 400

⚠ 階段 0 僅定義 + 單元測試，**無任何 handler 採用**（零行為變更，降低與平行 churn
衝突）。階段 1 才逐寫入點遷移、預設 enforce=False（soft，只 warning）；階段 2 enforce。

副作用界線：transition 只管「合法性閘 + 稽核」。各轉移的業務副作用（候補升正式設
promoted_at/confirm_deadline、過期清計時欄、重算 is_paid、容量重檢）仍由 caller 負責
——刻意不塞進 transition，以最小化改動面、避免遺漏既有副作用。

轉移表來源（2026-06-23 對現行碼核實，見 spec §3/§9）：
- RegistrationCourse：waitlist→{promoted_pending(auto),enrolled(manual)}；
  promoted_pending→{enrolled}（過期是 session.delete 刪列、非狀態轉移）；
  enrolled 終態（退課=刪列）。建立報名的初始 enrolled/waitlist 非轉移、走 builder。
- match_status：unmatched→{matched,pending}（公開報名比對）；
  pending→{matched(rematch),manual(人工綁),rejected(駁回)}；rejected→{pending}（restore）；
  matched/manual 視為已解析終態（階段 1 soft-enforce 若揭露合法漏邊再補）。
"""

from __future__ import annotations

import enum
import logging

logger = logging.getLogger(__name__)


class RegistrationCourseStatus(str, enum.Enum):
    """RegistrationCourse.status 值集合（值即現有字串）。"""

    ENROLLED = "enrolled"
    WAITLIST = "waitlist"
    PROMOTED_PENDING = "promoted_pending"


class MatchStatus(str, enum.Enum):
    """ActivityRegistration.match_status 值集合（值即現有字串）。"""

    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PENDING = "pending"
    REJECTED = "rejected"
    MANUAL = "manual"


# 佔容量狀態（取代 services/activity_service.OCCUPYING_STATUSES tuple 的型別化版本）。
OCCUPYING_COURSE_STATUSES: frozenset[RegistrationCourseStatus] = frozenset(
    {RegistrationCourseStatus.ENROLLED, RegistrationCourseStatus.PROMOTED_PENDING}
)


# {from: {合法 to 集合}}；終態為空集合。
RC_ALLOWED_TRANSITIONS: dict[
    RegistrationCourseStatus, frozenset[RegistrationCourseStatus]
] = {
    RegistrationCourseStatus.WAITLIST: frozenset(
        {RegistrationCourseStatus.PROMOTED_PENDING, RegistrationCourseStatus.ENROLLED}
    ),
    RegistrationCourseStatus.PROMOTED_PENDING: frozenset(
        {RegistrationCourseStatus.ENROLLED}
    ),
    RegistrationCourseStatus.ENROLLED: frozenset(),
}

MATCH_ALLOWED_TRANSITIONS: dict[MatchStatus, frozenset[MatchStatus]] = {
    MatchStatus.UNMATCHED: frozenset({MatchStatus.MATCHED, MatchStatus.PENDING}),
    MatchStatus.PENDING: frozenset(
        {MatchStatus.MATCHED, MatchStatus.MANUAL, MatchStatus.REJECTED}
    ),
    MatchStatus.REJECTED: frozenset({MatchStatus.PENDING}),
    MatchStatus.MATCHED: frozenset(),
    MatchStatus.MANUAL: frozenset(),
}


class ActivityTransitionError(ValueError):
    """非法狀態轉移（或非合法狀態值）。ValueError 子類 → 既有 handler 轉 HTTP 400。"""


def _coerce_rc(value) -> RegistrationCourseStatus:
    try:
        return RegistrationCourseStatus(value)
    except ValueError:
        raise ActivityTransitionError(
            f"非合法選課狀態：{value!r}（允許：{[s.value for s in RegistrationCourseStatus]}）"
        )


def _coerce_match(value) -> MatchStatus:
    try:
        return MatchStatus(value)
    except ValueError:
        raise ActivityTransitionError(
            f"非合法匹配狀態：{value!r}（允許：{[s.value for s in MatchStatus]}）"
        )


def is_rc_transition_allowed(from_status, to_status) -> bool:
    """純函式：RegistrationCourse.status from→to 是否合法（同態回 False）。"""
    f = _coerce_rc(from_status)
    t = _coerce_rc(to_status)
    if f == t:
        return False
    return t in RC_ALLOWED_TRANSITIONS.get(f, frozenset())


def is_match_transition_allowed(from_status, to_status) -> bool:
    """純函式：match_status from→to 是否合法（同態回 False）。"""
    f = _coerce_match(from_status)
    t = _coerce_match(to_status)
    if f == t:
        return False
    return t in MATCH_ALLOWED_TRANSITIONS.get(f, frozenset())


def _apply(
    *,
    kind: str,
    from_value,
    to_value,
    allowed_fn,
    enforce: bool,
) -> str:
    """共用：驗證 from→to；非法時 enforce=True raise、False 只 warning。回正規化 to 值字串。

    ⚠ Status Enum 是 str 子類，isinstance(x, str) 對 Enum member 也為 True，故須先判
    enum.Enum 才能正確抽 .value（否則 log/wire 寫成 'RegistrationCourseStatus.X'）。
    """
    from_str = (
        from_value.value if isinstance(from_value, enum.Enum) else str(from_value)
    )
    to_str = to_value.value if isinstance(to_value, enum.Enum) else str(to_value)
    if not allowed_fn(from_value, to_value):
        msg = f"不允許的{kind}狀態轉移：{from_str} → {to_str}"
        if enforce:
            raise ActivityTransitionError(msg)
        logger.warning("[activity-status soft-enforce] %s", msg)
    return to_str


def transition_registration_course_status(
    session,
    rc,
    to_status,
    *,
    operator: str,
    enforce: bool = False,
) -> None:
    """RegistrationCourse 狀態轉移：驗證 → 更新 status → 寫稽核。

    階段 1 採用時預設 enforce=False（soft：非法只 warning 不擋），階段 2 改 enforce=True。
    副作用（promoted_at/confirm_deadline/重算 is_paid 等）仍由 caller 負責。
    呼叫端負責 session.commit()。
    """
    to_value = _apply(
        kind="選課",
        from_value=rc.status,
        to_value=to_status,
        allowed_fn=is_rc_transition_allowed,
        enforce=enforce,
    )
    old = rc.status
    rc.status = to_value
    _log_status_change(
        session, rc.registration_id, "選課狀態轉移", old, to_value, operator
    )


def transition_match_status(
    session,
    reg,
    to_status,
    *,
    operator: str,
    enforce: bool = False,
) -> None:
    """ActivityRegistration.match_status 轉移：驗證 → 更新 → 寫稽核。語意同上。"""
    to_value = _apply(
        kind="匹配",
        from_value=reg.match_status,
        to_value=to_status,
        allowed_fn=is_match_transition_allowed,
        enforce=enforce,
    )
    old = reg.match_status
    reg.match_status = to_value
    _log_status_change(session, reg.id, "匹配狀態轉移", old, to_value, operator)


def _log_status_change(session, registration_id, change_type, old, new, operator):
    # 延遲 import 避免與 services.activity_service 的循環依賴。
    from services.activity_service import activity_service

    activity_service.log_change(
        session,
        registration_id,
        "",  # student_name：稽核僅記狀態變化，姓名非必要（避免額外查詢）
        change_type,
        f"{old} → {new}",
        operator or "",
    )
