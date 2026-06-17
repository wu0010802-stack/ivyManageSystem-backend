"""每個 Permission 都應被某個守衛/檢查引用，否則列入 KNOWN_UNENFORCED 白名單。

Why（設計稽核 2026-06-17, granularity ARCH-1）：granted-but-unenforced 的死權限
（已定義、發給角色、有標籤，但全強制層無任何引用）是 RBAC 形狀漂移的訊號，且與
「新增 Permission 後忘了接到端點守衛 → 非 wildcard admin 對該功能 403、admin UI 看得到
卻授了沒用」同源。本 sweep 在權限被定義卻從未被強制時於 CI fail。

偵測：掃強制層原始碼（api/ + services/ + utils/，排除 utils/permissions.py 自身的
定義/角色模板/標籤/分組），看每個 Permission 字串值是否以 word-boundary 出現。完全
未出現 → 視為「未被任何守衛引用」。

baseline 模式：白名單記錄當前已知的 orphan；新增任何不在白名單的 unenforced 權限即
fail，迫使開發者「把它接到守衛」或「有意識地加白名單並寫理由（或刪除該權限碼）」。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.permissions import Permission

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCAN_DIRS = ("api", "services", "utils")
# 排除權限定義檔本身：ROLE_TEMPLATES / PERMISSION_LABELS / PERMISSION_GROUPS 會引用
# 全部權限名，計入會掩蓋 orphan。
_EXCLUDE_SUFFIX = os.path.join("utils", "permissions.py")


# 已知「定義了但任何守衛/檢查都未引用」的權限（code → 理由）。
# 新增 orphan 預設應接到守衛；確需保留未強制者才加進此白名單並寫理由。
KNOWN_UNENFORCED: dict[str, str] = {
    "BUSINESS_ANALYTICS": (
        "經營分析權限：發給 supervisor/principal 模板且有標籤/分組，但全強制層"
        "（api/services/utils）無任何 require_*/has_permission 引用。待業主裁："
        "掛到實際『經營分析』端點守衛，或刪除此權限碼（granularity ARCH-1）。"
    ),
}


def _enforcement_blob() -> str:
    parts: list[str] = []
    for d in _SCAN_DIRS:
        base = os.path.join(_REPO_ROOT, d)
        for root, _dirs, files in os.walk(base):
            if "__pycache__" in root:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                if path.endswith(_EXCLUDE_SUFFIX):
                    continue
                with open(path, encoding="utf-8") as fh:
                    parts.append(fh.read())
    return "\n".join(parts)


def _unenforced_permissions() -> set[str]:
    blob = _enforcement_blob()
    return {
        p.value for p in Permission if not re.search(rf"\b{re.escape(p.value)}\b", blob)
    }


def test_no_new_unenforced_permission():
    """每個 Permission 都須被某個守衛/檢查引用，或明確列入 KNOWN_UNENFORCED 白名單。"""
    unenforced = _unenforced_permissions()
    new_orphans = sorted(unenforced - set(KNOWN_UNENFORCED))
    assert not new_orphans, (
        "以下 Permission 已定義但全強制層（api/services/utils）無任何守衛引用：\n"
        + "\n".join(f"  - {c}" for c in new_orphans)
        + "\n修法：把它接到實際端點守衛（require_permission/has_permission 等）；"
        "若刻意保留未強制或預定刪除，請加進 tests/test_permission_enforcement_coverage.py "
        "的 KNOWN_UNENFORCED 並寫理由。"
    )


def test_known_unenforced_has_no_stale_entries():
    """白名單不得有過時項：每個 KNOWN_UNENFORCED key 都應仍是『未被引用』的 orphan
    （該權限已接到守衛或被刪除時，提醒清掉白名單，避免它默默放行未來新洞）。"""
    unenforced = _unenforced_permissions()
    stale = sorted(set(KNOWN_UNENFORCED) - unenforced)
    assert (
        not stale
    ), "以下 KNOWN_UNENFORCED 白名單項已不再是 orphan（已被守衛引用或已移除），" "請從白名單刪除：\n" + "\n".join(
        f"  - {c}" for c in stale
    )


def test_known_unenforced_keys_are_real_permissions():
    """白名單 key 必須是合法 Permission（防打錯字讓守衛失效）。"""
    valid = {p.value for p in Permission}
    typos = sorted(set(KNOWN_UNENFORCED) - valid)
    assert not typos, f"KNOWN_UNENFORCED 含非法 Permission code：{typos}"
