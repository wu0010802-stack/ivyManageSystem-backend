from services.salary import config_defaults as cd
from services.salary import constants


def test_target_enrollment_uses_corrected_db_values():
    # 2026-06-25 業主裁定值（對齊 DB GradeTarget）
    assert cd.TARGET_ENROLLMENT["大班"]["2_teachers"] == 27
    assert cd.TARGET_ENROLLMENT["大班"]["1_teacher"] == 14
    assert cd.TARGET_ENROLLMENT["中班"]["2_teachers"] == 25
    assert cd.TARGET_ENROLLMENT["中班"]["1_teacher"] == 13
    assert cd.TARGET_ENROLLMENT["小班"]["2_teachers"] == 23


def test_constants_reexports_config_defaults():
    # 同一物件 → 單一來源；改 config_defaults 即改 constants
    assert constants.TARGET_ENROLLMENT is cd.TARGET_ENROLLMENT
    assert constants.FESTIVAL_BONUS_BASE is cd.FESTIVAL_BONUS_BASE
    assert constants.SUPERVISOR_DIVIDEND is cd.SUPERVISOR_DIVIDEND
    assert constants.OVERTIME_TARGET is cd.OVERTIME_TARGET
    assert constants.OVERTIME_BONUS_PER_PERSON is cd.OVERTIME_BONUS_PER_PERSON
    assert constants.SUPERVISOR_FESTIVAL_BONUS is cd.SUPERVISOR_FESTIVAL_BONUS
    assert constants.OFFICE_FESTIVAL_BONUS_BASE is cd.OFFICE_FESTIVAL_BONUS_BASE
    assert constants.POSITION_GRADE_MAP is cd.POSITION_GRADE_MAP
