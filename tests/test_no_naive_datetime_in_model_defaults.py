"""Phase 3 lint coverage：Ruff DTZ 抓不到 model `default=datetime.now`
(callable reference 非 call expression)，用 reflection 補。

PR3 完成後 MODEL_DEFAULT_ALLOWLIST 應為 empty set；新增 model column
用 default=datetime.now 即測試紅。

Note: 以 __qualname__ 比對而非物件 identity，因為 datetime.now 在 Python 中
是 built-in method，各 model 模組 import 時捕捉的 function object 位址不同，
identity (is / in set) 比對會全部失敗。
"""

import pytest
from sqlalchemy import inspect

# 觸發所有 model import → 註冊到 Base.registry
# models/__init__.py 只 re-export 部分 model，必須逐一 import 才能讓
# Base.registry 收錄全部 mapper。
import models  # noqa: F401
import models.academic_term  # noqa: F401
import models.activity  # noqa: F401
import models.appraisal  # noqa: F401
import models.approval  # noqa: F401
import models.art_teacher_payroll  # noqa: F401
import models.attendance  # noqa: F401
import models.audit  # noqa: F401
import models.auth  # noqa: F401
import models.classroom  # noqa: F401
import models.config  # noqa: F401
import models.contact_book  # noqa: F401
import models.disciplinary  # noqa: F401
import models.dismissal  # noqa: F401
import models.employee  # noqa: F401
import models.event  # noqa: F401
import models.fees  # noqa: F401
import models.gov_moe  # noqa: F401
import models.guardian  # noqa: F401
import models.leave  # noqa: F401
import models.line_config  # noqa: F401
import models.monthly_fixed_cost  # noqa: F401
import models.notification_log  # noqa: F401
import models.offboarding  # noqa: F401
import models.overtime  # noqa: F401
import models.overtime_comp_leave_grant  # noqa: F401
import models.parent_binding  # noqa: F401
import models.parent_db  # noqa: F401
import models.parent_message  # noqa: F401
import models.parent_notification  # noqa: F401
import models.parent_refresh_token  # noqa: F401
import models.permission_models  # noqa: F401
import models.portfolio  # noqa: F401
import models.recruitment  # noqa: F401
import models.report_cache  # noqa: F401
import models.salary  # noqa: F401
import models.security  # noqa: F401
import models.shift  # noqa: F401
import models.student_leave  # noqa: F401
import models.student_log  # noqa: F401
import models.student_transfer  # noqa: F401
import models.unused_leave_payout_log  # noqa: F401
import models.vendor_payment  # noqa: F401
import models.year_end  # noqa: F401
from models.base import Base

# __qualname__ 比對，而非物件 identity：
# datetime.now 是 built-in method；各模組 import 後捕捉的 function object
# 與 from datetime import datetime 拿到的 datetime.now 位址不同。
FORBIDDEN_QUALNAMES = {"datetime.now", "datetime.utcnow"}


def _is_forbidden(arg) -> bool:
    """回傳 True 若 callable arg 的 __qualname__ 在禁止清單中。"""
    if not callable(arg):
        return False
    return getattr(arg, "__qualname__", None) in FORBIDDEN_QUALNAMES


# PR1 初始填入；PR3 已全數替換，allow-list 清空
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = set()
# Total: 0 (PR3 cleared)


def _collect_violations() -> list[tuple[str, str]]:
    """走訪所有 model column 找出 default / onupdate callable in FORBIDDEN_QUALNAMES.

    同時檢查 column.default 和 column.onupdate，因為
    onupdate=datetime.now 與 default=datetime.now 有同樣的 TZ 問題。
    """
    violations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for col in inspect(cls).columns:
            for attr in ("default", "onupdate"):
                cd = getattr(col, attr, None)
                if cd is None:
                    continue
                arg = getattr(cd, "arg", None)
                if _is_forbidden(arg):
                    key = (cls.__name__, col.name)
                    if key not in seen:
                        seen.add(key)
                        violations.append(key)
    return violations


def test_no_naive_datetime_in_model_defaults():
    violations = _collect_violations()
    unauthorized = [v for v in violations if v not in MODEL_DEFAULT_ALLOWLIST]
    assert not unauthorized, (
        "Model column default / onupdate 用了 datetime.now / utcnow，"
        "請改用 utils.taipei_time.now_taipei_naive():\n"
        + "\n".join(f"  - {cls}.{col}" for cls, col in unauthorized)
    )


def test_model_default_allowlist_is_empty():
    assert MODEL_DEFAULT_ALLOWLIST == set(), (
        "PR3 收尾必須把 MODEL_DEFAULT_ALLOWLIST 清空。"
        f"剩餘：{sorted(MODEL_DEFAULT_ALLOWLIST)}"
    )
