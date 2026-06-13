"""Spec C: PIIRedactionFilter 6 pytest。

覆蓋：
1. record.msg PII key=value 被 redact
2. record.msg non-PII key=value 保留
3. record.args dict 內 PII keys 被 scrub
4. exc_info exception args 內 PII 被 redact
5. non-string msg 不報錯
6. filter return True（不擋 record）
"""

import io
import logging

import pytest

from utils.log_pii_filter import PIIRedactionFilter


@pytest.fixture
def logger_with_filter():
    """建獨立 logger 加 PIIRedactionFilter + StringIO handler 捕捉輸出。"""
    log = logging.getLogger("test_pii_filter")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(PIIRedactionFilter())
    log.addHandler(handler)

    yield log, stream
    log.handlers.clear()


def test_msg_with_pii_key_value_redacted(logger_with_filter):
    """student_name=小明 → student_name=[Filtered]。"""
    log, stream = logger_with_filter
    log.warning("user student_name=小明 logged in")
    out = stream.getvalue()
    assert "student_name=[Filtered]" in out
    assert "小明" not in out


def test_msg_with_non_pii_key_value_kept(logger_with_filter):
    """request_id (exempt) + student_id (non-PII key) 保留。"""
    log, stream = logger_with_filter
    log.warning("request request_id=abc123 student_id=42 path=/api/foo")
    out = stream.getvalue()
    assert "request_id=abc123" in out
    assert "student_id=42" in out
    assert "[Filtered]" not in out


def test_args_dict_with_pii_scrubbed(logger_with_filter):
    """logger.warning(msg, dict_args) 內 dict PII keys 被遮。"""
    log, stream = logger_with_filter
    log.warning(
        "update %(student_name)s %(salary)s",
        {"student_name": "小明", "salary": 50000},
    )
    out = stream.getvalue()
    assert "小明" not in out
    assert "50000" not in out
    assert "[Filtered]" in out


def test_exc_info_args_redacted(logger_with_filter):
    """exception args 內 PII（phone=...）被遮。

    filter 改寫 exc.args，所以 traceback 末行的 `ValueError: ...` 顯示 [Filtered]。
    但 Python traceback formatter 也會印出 raise 的原始碼行（含 literal），
    所以只驗證 exception message 行已被 redact，不驗證整個 output 不含原號碼。
    """
    log, stream = logger_with_filter
    try:
        raise ValueError("phone=0912345678 invalid")
    except ValueError:
        log.exception("validation failed")
    out = stream.getvalue()
    # exception 的 message（最後一行 "ValueError: ..."）應已被 redact
    assert "phone=[Filtered]" in out


def test_non_string_msg_not_modified(logger_with_filter):
    """logger.warning(dict 物件) → 不報錯，filter 只處理 str msg。"""
    log, stream = logger_with_filter
    # Should not raise; filter must handle non-string msg gracefully
    log.warning({"already_dict": True})  # type: ignore[arg-type]


def test_filter_returns_true_does_not_block(logger_with_filter):
    """Filter return True，record 通過不被擋。"""
    log, stream = logger_with_filter
    log.warning("test message no pii")
    assert "test message no pii" in stream.getvalue()


def test_positional_pii_args_redacted_in_final_output(logger_with_filter):
    """logger.warning("guardian_id=%s user_id=%s", gid, uid)：

    回歸 — 舊版對 raw format string 跑 redact，把 `guardian_id=%s` 的 `%s`
    placeholder 一起抹掉，handler 做 `msg % args` 時 placeholder 數 < args 數
    → TypeError → 端點 500（且實際 PII 未遮）。修法（format→redact→clear args）
    須在「最終輸出字串」遮掉真實值且不 crash。
    """
    log, stream = logger_with_filter
    log.warning("parent bind guardian_id=%s phone=%s done", 123456, "0912345678")
    out = stream.getvalue()
    # 真實值不可外洩
    assert "123456" not in out
    assert "0912345678" not in out
    # PII key 的值被遮
    assert "guardian_id=[Filtered]" in out
    assert "phone=[Filtered]" in out
    # 非 PII 文字保留、未 crash（有完整輸出）
    assert "parent bind" in out and "done" in out


# ── S4（2026-06-13）：SQLAlchemy StatementError parameters dict 格式 ─────────


def test_sqlalchemy_parameters_dict_pii_redacted(logger_with_filter):
    """SQLAlchemy StatementError 的 [parameters: {'key': 'value'}] 為
    `'key': 'value'` 格式（非 key=value），PII value 也必須被遮。"""
    log, stream = logger_with_filter
    log.error(
        "(sqlite3.IntegrityError) NOT NULL constraint failed "
        "[parameters: {'student_name': '王小明', 'parent_phone': '0912345678', "
        "'status': 'pending'}]"
    )
    out = stream.getvalue()
    assert "王小明" not in out
    assert "0912345678" not in out
    assert "[Filtered]" in out
    # 非 PII key 的 value 保留（debug context）
    assert "pending" in out


def test_double_quoted_parameters_dict_pii_redacted(logger_with_filter):
    """雙引號 JSON-style `"key": "value"` 格式同樣命中。"""
    log, stream = logger_with_filter
    log.error('payload={"parent_phone": "0912-345-678", "status": "ok"}')
    out = stream.getvalue()
    assert "0912-345-678" not in out
    assert "[Filtered]" in out
    assert '"status": "ok"' in out


def test_exc_info_sqlalchemy_parameters_redacted(logger_with_filter):
    """exc_info 路徑（exception.args 內含 parameters dict 字串）同樣生效。"""
    log, stream = logger_with_filter
    # 動態組值：避免 traceback 的「source line 回顯」也含字面值（真實情境中
    # SQL 參數為 runtime 值，不會出現在 source line）
    params = {"student_name": "小" + "明", "phone": "0987" + "654321"}
    try:
        raise ValueError(f"StatementError [parameters: {params!r}]")
    except ValueError:
        log.error("DB 寫入失敗", exc_info=True)
    out = stream.getvalue()
    assert "小明" not in out
    assert "0987654321" not in out
    assert "[Filtered]" in out


def test_parameters_dict_non_pii_keys_untouched(logger_with_filter):
    """全部都是非 PII key 時 dict 字串原樣保留。"""
    log, stream = logger_with_filter
    log.warning("[parameters: {'status': 'late', 'amount': 500}]")
    out = stream.getvalue()
    assert "'status': 'late'" in out
    assert "[Filtered]" not in out
