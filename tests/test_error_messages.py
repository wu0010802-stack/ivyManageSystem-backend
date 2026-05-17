"""tests/test_error_messages.py — utils/error_messages 常數測試。

僅含字串常數，無 helper。測試確保：
1. 所有 expected 常數存在且為非空 str
2. 中文訊息內容大致符合語意（避免被誤改成空字串或英文）
"""

from utils import error_messages as em

EXPECTED_CONSTANTS = {
    "STUDENT_NOT_FOUND": "找不到該學生",
    "EMPLOYEE_NOT_FOUND": "找不到該員工",
    "EMPLOYEE_DOES_NOT_EXIST": "員工不存在",
    "USER_NOT_FOUND": "使用者不存在",
    "CLASSROOM_NOT_FOUND": "找不到該班級",
    "ANNOUNCEMENT_NOT_FOUND": "找不到該公告",
    "LEAVE_RECORD_NOT_FOUND": "請假記錄不存在",
    "OVERTIME_RECORD_NOT_FOUND": "加班記錄不存在",
    "SALARY_RECORD_NOT_FOUND": "薪資記錄不存在",
    "EVENT_NOT_FOUND": "找不到該事件",
}


class TestErrorMessageConstants:
    def test_all_expected_constants_exist(self):
        for name in EXPECTED_CONSTANTS:
            assert hasattr(em, name), f"缺少常數 {name}"

    def test_all_constants_have_expected_value(self):
        for name, expected in EXPECTED_CONSTANTS.items():
            assert (
                getattr(em, name) == expected
            ), f"{name} 值不符（請確認是否被意外改動）"

    def test_all_messages_are_non_empty_strings(self):
        for name in EXPECTED_CONSTANTS:
            value = getattr(em, name)
            assert isinstance(value, str)
            assert value.strip(), f"{name} 不可為空字串"

    def test_messages_are_chinese(self):
        # 防止被誤改成英文或 placeholder
        for name in EXPECTED_CONSTANTS:
            value = getattr(em, name)
            # 至少包含一個 CJK 字
            assert any("一" <= ch <= "鿿" for ch in value), f"{name} 應為繁體中文訊息"

    def test_not_found_keywords_in_messages(self):
        # 既然命名都是 *_NOT_FOUND 或 *_DOES_NOT_EXIST，訊息應含「不」字
        for name in EXPECTED_CONSTANTS:
            assert "不" in getattr(em, name), f"{name} 應為否定語意訊息"
