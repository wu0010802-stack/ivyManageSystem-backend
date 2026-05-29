"""ErrorCode enum 註冊測試。"""

from utils.error_codes import ErrorCode


def test_error_code_enum_values():
    assert ErrorCode.BIND_CODE_INVALID.value == "BIND_CODE_INVALID"
    assert ErrorCode.LINE_BINDING_EXPIRED.value == "LINE_BINDING_EXPIRED"
    assert ErrorCode.STUDENT_NOT_FOUND.value == "STUDENT_NOT_FOUND"


def test_error_code_no_duplicates():
    values = [e.value for e in ErrorCode]
    assert len(values) == len(set(values))


def test_error_code_count():
    """目前註冊 15 個 family；新增需同步前端 errorCodeRegistry.ts。"""
    assert len(list(ErrorCode)) == 15


def test_existing_bind_code_aliases_registered():
    """既有 auth.py _diagnose_binding_failure 用的 code 必納入 registry。"""
    assert ErrorCode.BIND_CODE_NOT_FOUND.value == "BIND_CODE_NOT_FOUND"
    assert ErrorCode.BIND_CODE_USED.value == "BIND_CODE_USED"


def test_error_code_is_str_enum():
    """str Enum：value 即字串，方便序列化與比對。"""
    assert isinstance(ErrorCode.BIND_CODE_INVALID.value, str)
    assert ErrorCode.BIND_CODE_INVALID == "BIND_CODE_INVALID"
