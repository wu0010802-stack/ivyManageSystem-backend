"""
節慶獎金與超額獎金純函式計算
所有函式接受 config dict 作為參數，不依賴 SalaryEngine 實例。
"""

import logging
from datetime import date, datetime
from typing import Optional
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


# 階段 2-D（2026-05-07）：職稱→等級對應改採 module-level cache，
# 由 SalaryEngine 在 load_config_from_db 時透過 set_active_grade_map 注入。
# 預設指向 hardcode POSITION_GRADE_MAP 作為 test/cold-start fallback。
_active_grade_map: Optional[dict] = None


def set_active_grade_map(grade_map: Optional[dict]) -> None:
    """由 SalaryEngine 注入「職稱→等級」對應（從 job_titles.bonus_grade 讀）。

    None 視為清除注入，恢復使用 constants.POSITION_GRADE_MAP fallback。
    呼叫端負責執行緒安全（engine 載入路徑已有 _config_swap_lock）。
    """
    global _active_grade_map
    _active_grade_map = grade_map


def _resolve_grade_map(grade_map: Optional[dict] = None) -> dict:
    """決定使用哪份對應表：caller 帶入 > module cache > hardcode fallback。"""
    if grade_map is not None:
        return grade_map
    if _active_grade_map is not None:
        return _active_grade_map
    from .constants import POSITION_GRADE_MAP

    return POSITION_GRADE_MAP


def get_position_grade(
    position: str, grade_map: Optional[dict] = None
) -> Optional[str]:
    """取得職位等級 (A/B/C)。grade_map 為 None 時走 _resolve_grade_map fallback chain。"""
    return _resolve_grade_map(grade_map).get(position)


def get_festival_bonus_base(position: str, role: str, bonus_base: dict) -> float:
    """
    取得節慶獎金基數

    Args:
        position:   職位 (幼兒園教師/教保員/助理教保員/職員)
        role:       角色 (head_teacher/assistant_teacher)
        bonus_base: 獎金基數 config dict（self._bonus_base）
    Returns:
        獎金基數
    """
    grade = get_position_grade(position)  # 走 module cache fallback chain
    if role not in bonus_base:
        return 0

    # 如果沒有對應的職位等級，預設使用 C 級。
    # 正常分流（office/supervisor 走獨立路徑）下不應觸發；
    # 觸發代表分流誤判或 position 拼字異常，記 warning 供 ops 排查。
    # 行為維持 fallback 'C' 以避免突然改變既有薪資金額。
    if not grade:
        logger.warning(
            "節慶獎金：未知職位 position=%r 不在 POSITION_GRADE_MAP，fallback 為 C 級。"
            "若該員工非帶班老師，請確認 _calculate_bonuses 分流邏輯與 office_staff_context。",
            position,
        )
        grade = "C"

    # 使用 `or 0` 防禦 DB 欄位為 NULL 的情況：
    # dict.get(grade, 0) 在 key 存在但值為 None 時仍回傳 None（非預設 0），
    # None * ratio 會拋 TypeError，導致整批薪資中斷。
    return bonus_base[role].get(grade, 0) or 0


def get_target_enrollment(
    grade_name: str,
    has_assistant: bool,
    is_shared_assistant: bool,
    target_map: dict,
) -> int:
    """
    取得目標人數

    Args:
        grade_name:         年級名稱 (大班/中班/小班/幼幼班)
        has_assistant:      班級是否有副班導
        is_shared_assistant: 是否為共用美師
        target_map:         目標人數 config dict（self._target_enrollment）
    Returns:
        目標人數
    """
    if grade_name not in target_map:
        return 0

    targets = target_map[grade_name]

    if is_shared_assistant:
        return targets.get("shared_assistant", 0)
    elif has_assistant:
        return targets.get("2_teachers", 0)
    else:
        return targets.get("1_teacher", 0)


def get_supervisor_dividend(
    title: str, position: str, dividend_map: dict, supervisor_role: str = ""
) -> float:
    """
    取得主管紅利

    Args:
        title:        教育局系統職稱
        position:     園內實際職位，也會檢查
        dividend_map: 主管紅利 config dict（self._supervisor_dividend）
        supervisor_role: 主管職設定（園長/主任/組長/副組長）
    Returns:
        紅利金額，若非主管職則返回 0
    """
    if supervisor_role in dividend_map:
        return dividend_map[supervisor_role]
    if position in dividend_map:
        return dividend_map[position]
    if title in dividend_map:
        return dividend_map[title]
    return 0


def get_supervisor_festival_bonus(
    title: str, position: str, bonus_map: dict, supervisor_role: str = ""
) -> Optional[float]:
    """
    取得主管節慶獎金基數

    Args:
        title:     教育局系統職稱
        position:  園內實際職位，也會檢查
        bonus_map: 主管節慶獎金基數 config dict（self._supervisor_festival_bonus）
        supervisor_role: 主管職設定（園長/主任/組長）
    Returns:
        節慶獎金基數，若非主管職則返回 None
    """
    if supervisor_role in bonus_map:
        return bonus_map[supervisor_role]
    if position in bonus_map:
        return bonus_map[position]
    if title in bonus_map:
        return bonus_map[title]
    return None


def get_office_festival_bonus_base(
    position: str, title: str, office_map: dict
) -> Optional[float]:
    """
    取得司機/美編節慶獎金基數

    Args:
        position:  職稱 (司機/美編)
        title:     職務，也會檢查
        office_map: 辦公室節慶獎金基數 config dict（self._office_festival_bonus_base）
    Returns:
        節慶獎金基數，若非司機/美編則返回 None
    """
    if position in office_map:
        return office_map[position]
    if title in office_map:
        return office_map[title]
    return None


def get_overtime_target(
    grade_name: str,
    has_assistant: bool,
    is_shared_assistant: bool,
    target_map: dict,
) -> int:
    """取得超額獎金目標人數"""
    if grade_name not in target_map:
        return 0

    targets = target_map[grade_name]

    if is_shared_assistant:
        return targets.get("shared_assistant", 0)
    elif has_assistant:
        return targets.get("2_teachers", 0)
    else:
        return targets.get("1_teacher", 0)


def get_overtime_per_person(role: str, grade_name: str, per_person_map: dict) -> float:
    """取得超額獎金每人金額"""
    if role not in per_person_map:
        return 0
    return per_person_map[role].get(grade_name, 0)


def is_eligible_for_festival_bonus(
    hire_date, reference_date=None, festival_months: int = 3
) -> bool:
    """
    檢查員工是否符合領取節慶獎金資格（入職滿N個月）

    Args:
        hire_date:       到職日期 (date 或 str 格式 'YYYY-MM-DD')
        reference_date:  參考日期，預設為今天
        festival_months: 需滿幾個月，預設 3
    Returns:
        True 如果入職滿N個月，否則 False
    """
    if hire_date is None:
        return True  # 如果沒有到職日期資料，預設可以領

    if isinstance(hire_date, str):
        try:
            hire_date = datetime.strptime(hire_date, "%Y-%m-%d").date()
        except ValueError:
            return True  # 日期格式錯誤，預設可以領

    if reference_date is None:
        reference_date = date.today()
    elif isinstance(reference_date, str):
        reference_date = datetime.strptime(reference_date, "%Y-%m-%d").date()

    # 計算入職滿N個月的日期
    eligible_date = hire_date + relativedelta(months=festival_months)

    return reference_date >= eligible_date


def calculate_overtime_bonus(
    role: str,
    grade_name: str,
    current_enrollment: int,
    has_assistant: bool,
    is_shared_assistant: bool,
    overtime_target_map: dict,
    overtime_per_person_map: dict,
) -> dict:
    """
    計算超額獎金

    Args:
        role:                   角色 (head_teacher/assistant_teacher/art_teacher)
        grade_name:             年級名稱
        current_enrollment:     在籍人數
        has_assistant:          班級是否有副班導
        is_shared_assistant:    是否為共用美師
        overtime_target_map:    超額目標人數 config dict
        overtime_per_person_map: 超額每人金額 config dict
    Returns:
        包含 overtime_bonus, overtime_target, overtime_count, per_person 的字典
    """
    # 美師特別處理
    if role == "art_teacher":
        is_shared_assistant = True
        role_for_bonus = "assistant_teacher"
    else:
        role_for_bonus = role

    # 取得超額目標人數
    overtime_target = get_overtime_target(
        grade_name, has_assistant, is_shared_assistant, overtime_target_map
    )

    # 計算超額人數
    overtime_count = max(0, current_enrollment - overtime_target)

    # 取得每人金額
    per_person = get_overtime_per_person(
        role_for_bonus, grade_name, overtime_per_person_map
    )

    # 計算超額獎金
    overtime_bonus = overtime_count * per_person

    return {
        "overtime_bonus": round(overtime_bonus),
        "overtime_target": overtime_target,
        "overtime_count": overtime_count,
        "per_person": per_person,
    }


def calculate_festival_bonus_v2(
    position: str,
    role: str,
    grade_name: str,
    current_enrollment: int,
    has_assistant: bool,
    is_shared_assistant: bool,
    bonus_base: dict,
    target_enrollment_map: dict,
    overtime_target_map: dict,
    overtime_per_person_map: dict,
) -> dict:
    """
    計算節慶獎金 (新版 - 依職位等級和角色計算)

    Args:
        position:               職位 (幼兒園教師/教保員/助理教保員)
        role:                   角色 (head_teacher/assistant_teacher/art_teacher)
        grade_name:             年級名稱
        current_enrollment:     在籍人數
        has_assistant:          班級是否有副班導
        is_shared_assistant:    是否為共用美師 (美師)
        bonus_base:             獎金基數 config dict
        target_enrollment_map:  節慶獎金目標人數 config dict
        overtime_target_map:    超額目標人數 config dict
        overtime_per_person_map: 超額每人金額 config dict
    Returns:
        包含 festival_bonus, overtime_bonus, target, ratio 等的字典
    """
    # 美師特別處理：用 shared_assistant 的目標人數
    if role == "art_teacher":
        is_shared_assistant = True
    role_for_bonus = role

    # 取得獎金基數（art_teacher 在 FESTIVAL_BONUS_BASE 有獨立基數 2000）
    base_amount = get_festival_bonus_base(position, role_for_bonus, bonus_base)

    # 取得節慶獎金目標人數
    target = get_target_enrollment(
        grade_name, has_assistant, is_shared_assistant, target_enrollment_map
    )

    # 計算比例和節慶獎金
    if target > 0:
        ratio = current_enrollment / target
        festival_bonus = base_amount * ratio
    else:
        ratio = 0
        festival_bonus = 0

    # 計算超額獎金
    overtime_result = calculate_overtime_bonus(
        role=role,
        grade_name=grade_name,
        current_enrollment=current_enrollment,
        has_assistant=has_assistant,
        is_shared_assistant=is_shared_assistant,
        overtime_target_map=overtime_target_map,
        overtime_per_person_map=overtime_per_person_map,
    )

    return {
        "festival_bonus": round(festival_bonus),
        "overtime_bonus": overtime_result["overtime_bonus"],
        "target": target,
        "ratio": ratio,
        "base_amount": base_amount,
        "overtime_target": overtime_result["overtime_target"],
        "overtime_count": overtime_result["overtime_count"],
        "overtime_per_person": overtime_result["per_person"],
    }
