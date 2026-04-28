"""tests/test_safe_500.py — MEDIUM-1 回歸測試

驗證兩件事：
1. raise_safe_500() 在 production env 下不洩漏原始例外訊息
2. SECURITY_AUDIT.md 列出的 6 個公開 router + 已知熱點，不再使用
   `HTTPException(status_code=500, detail=str(e))` 或 detail 含 f-string `{e}`
"""

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── 部分 1：raise_safe_500 行為 ─────────────────────────────────────


def _reload_errors_with_env(env_value: str):
    os.environ["ENV"] = env_value
    from utils import errors

    return importlib.reload(errors)


def test_raise_safe_500_production_does_not_leak_message():
    errors = _reload_errors_with_env("production")
    try:
        errors.raise_safe_500(ValueError("DB column user_id missing"))
    except Exception as exc:
        # FastAPI HTTPException
        assert hasattr(exc, "detail")
        detail = str(exc.detail)
        assert "DB column" not in detail
        assert "user_id" not in detail
        assert detail == "系統內部錯誤，請聯繫管理員"


def test_raise_safe_500_development_includes_message_for_debugging():
    errors = _reload_errors_with_env("development")
    try:
        errors.raise_safe_500(ValueError("dev-only message"))
    except Exception as exc:
        assert "dev-only message" in str(exc.detail)


# ── 部分 2：靜態檢查 — 不再使用洩漏 pattern ─────────────────────────


_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# audit 列出的 6 個檔案 + pos.py（同性質補修）
_AUDIT_FILES = [
    "api/activity/public.py",
    "api/activity/registrations.py",
    "api/activity/settings.py",
    "api/activity/supplies.py",
    "api/activity/courses.py",
    "api/activity/inquiries.py",
    "api/activity/pos.py",
]

# 非常確切的洩漏 pattern：HTTPException(status_code=500, detail=str(e)) 或 f-string with {e}
# 注意：detail 中含合理的字面字串（如「結帳失敗」）但不洩漏例外內容是 OK 的；
# 我們只攔「detail 直接放 str(e) / repr(e) / f"...{e}"」。
_LEAK_PATTERNS = [
    re.compile(r"status_code\s*=\s*500[^)]*detail\s*=\s*str\s*\(\s*e\s*\)", re.S),
    re.compile(r"status_code\s*=\s*500[^)]*detail\s*=\s*repr\s*\(\s*e\s*\)", re.S),
    re.compile(
        r"status_code\s*=\s*500[^)]*detail\s*=\s*f[\"'][^\"']*\{\s*e\s*\}", re.S
    ),
]


@pytest.mark.parametrize("rel_path", _AUDIT_FILES)
def test_audit_files_do_not_leak_500_exception_messages(rel_path):
    full = _BACKEND_ROOT / rel_path
    assert full.exists(), f"file not found: {rel_path}"
    src = full.read_text(encoding="utf-8")
    for pat in _LEAK_PATTERNS:
        match = pat.search(src)
        assert match is None, (
            f"{rel_path} 仍存在 500 例外訊息洩漏 pattern：{match.group(0)[:80]!r}\n"
            f"請改用 `raise_safe_500(e, context=...)`"
        )
