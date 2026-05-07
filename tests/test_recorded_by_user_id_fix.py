"""驗證學生事件/評量/出席的 recorded_by 欄位正確寫入 user.id（不再永遠 NULL）。

威脅：current_user 從 JWT decode 得到的 dict 只有 'user_id' key，沒有 'id'。
原本多處 `current_user.get("id")` 永遠 None，導致：
- StudentIncident.recorded_by = NULL（事件登錄者匿名 → 稽核軌跡失效）
- StudentAssessment.recorded_by = NULL
- StudentAttendance 教師端紀錄 recorded_by = NULL

Refs: 邏輯漏洞 audit 2026-05-07 P1（cross-file 同類 bug）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestNoStaleIdKeyAccess:
    """純靜態檢查：codebase 不應再有 current_user.get("id") 的用法。"""

    def test_no_current_user_get_id_remaining(self):
        """current_user 永遠用 get("user_id")，不會用 get("id")。"""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for sub in ("api", "services", "utils"):
            for py in (root / sub).rglob("*.py"):
                # 跳過 venv 等
                if "__pycache__" in py.parts or "venv" in py.parts:
                    continue
                try:
                    text = py.read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in re.finditer(
                    r'current_user\.get\(["\']id["\']\)|current_user\[["\']id["\']\]',
                    text,
                ):
                    line_no = text[: m.start()].count("\n") + 1
                    offenders.append(f"{py.relative_to(root)}:{line_no}")

        assert not offenders, (
            "以下位置仍使用 current_user.get('id')；應改為 .get('user_id')。\n"
            "JWT payload 沒有 'id' key（見 utils/auth.py:357），永遠回 None：\n  "
            + "\n  ".join(offenders)
        )
