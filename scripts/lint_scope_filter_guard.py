"""Scope-filter 守衛：靜態確保每個「gate 用 row-level scope-aware 權限」的 API
端點都有套 scope 過濾（呼叫 scope helper 或鎖 :all），避免 fail-open。

背景
----
權限以字串集合存於 ``User.permission_names``，row-level scoping 用 wire format
``<CODE>:<scope>``（``own_class`` / ``all``）。``require_*_permission`` 的 gate
是 **scope-blind**——它放行任何持有 ``CODE:own_class`` 的使用者；真正的 row
過濾靠 router **主動**呼叫 ``utils.portfolio_access`` 的 helper。兩步分離，第二步
沒有任何強制機制：**漏套 filter 即 fail-open（端點回傳全園資料）**。

「這個權限是 scope-aware」這件事原本散落在三個會各自漂移的地方：
``permissions._SCOPE_AWARE_PREFIXES``、``permission_definitions.scope_options``
DB 欄、以及各端點的 filter 呼叫。本 lint 把三者收斂成一個會自動失敗的檢查，
institutionalize「row-level scoping 必驗 gate + filter 雙路徑」的慣例。

用法
----
    python scripts/lint_scope_filter_guard.py        # 掃 api/，有違規則 exit 1

測試：``tests/test_scope_filter_guard.py``（對 fixture + 真實 codebase 斷言）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 權威清單：permission_definitions.scope_options 非空的 13 個權限
# （alembic permscope01-04 seed）。新增 scope-aware 權限時必須同步此處，
# 否則新權限的端點不會被守衛檢查。
# ---------------------------------------------------------------------------
SCOPE_AWARE_PERMS = frozenset(
    {
        "STUDENTS_READ",
        "STUDENTS_WRITE",
        "STUDENTS_LIFECYCLE_WRITE",
        "PORTFOLIO_READ",
        "PORTFOLIO_WRITE",
        "PORTFOLIO_PUBLISH",
        "STUDENTS_HEALTH_READ",
        "STUDENTS_HEALTH_WRITE",
        "STUDENTS_SPECIAL_NEEDS_READ",
        "STUDENTS_SPECIAL_NEEDS_WRITE",
        "STUDENTS_MEDICATION_ADMINISTER",
        "DISMISSAL_CALLS_READ",
        "DISMISSAL_CALLS_WRITE",
    }
)

# 端點 body（或其遞迴呼叫的同檔 helper）引用下列任一「葉子原語」即視為
# 「有套 scope 過濾」。葉子 = 真正做 row/班級過濾或 scope 解析的根函式。
#
# 注意：codebase 目前有多套並存的 scope 機制（碎片化，本身是優化點）：
#   - utils.portfolio_access 中央 helper（首選）
#   - utils.permissions 的 scope 解析原語
#   - api/portal/_shared._get_teacher_classroom_ids（班級反查根）
#   - 各檔 inline 自實作的班級反查私有 helper（複製三欄 OR；應重構掉）
# 葉子清單必須涵蓋全部，否則合規端點會被誤報 fail-open。新增 scope 機制
# 時同步此清單。delegate wrapper（如 _scoped_query→_student_ids_in_scope→
# student_ids_in_scope）由遞迴追蹤自動涵蓋，不需列此。
SCOPE_HELPER_NAMES = frozenset(
    {
        # --- utils.portfolio_access 根 helper ---
        "is_unrestricted",
        "accessible_classroom_ids",
        "assert_student_access",
        "filter_student_ids_by_access",
        "student_ids_in_scope",
        "require_unrestricted_role",
        "get_owned_resource_or_403",
        "assert_all_scope",  # 全園彙總端點鎖 :all（堵 fail-open，零行為變更）
        # --- utils.permissions scope 解析原語 ---
        "resolve_grant",
        "require_scoped_permission",
        # --- portal 班級/學生反查根 ---
        "_get_teacher_classroom_ids",
        "_get_teacher_student_ids",
        # --- 已知 inline 班級反查葉子（複製三欄 OR；TODO 重構為
        #     accessible_classroom_ids，列此避免合規端點被誤報）---
        "_require_classroom_access",
        "_assert_classroom_owned",
    }
)

# gate dependency 的函式名（require_*_permission 家族）。
GATE_FUNCS = frozenset(
    {
        "require_permission",
        "require_staff_permission",
        "require_scoped_permission",
    }
)

# FastAPI router decorator 的 HTTP 動詞。
_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}
)

# ---------------------------------------------------------------------------
# 明確豁免：endpoint 識別碼 "<module-stem>:<func-name>" → 原因。
# 僅用於「gate 借用 scope-aware 權限、但資源本身非 student row-scoped」的端點；
# 不可用來掩蓋真正漏套 filter 的 fail-open。每筆都必須附可稽核的原因。
# ---------------------------------------------------------------------------
EXEMPT: dict[str, str] = {
    # 薪資獎金影響預覽 / 儀表板：以薪資引擎為單位，非學生 row-scoped 資源。
    # 目前借用 STUDENTS_* gate（設計債）。TODO(perm-rename): 正名為
    # SALARY_WRITE / SALARY_READ（需同步前端 ROUTE_PERMISSION_RULES + 角色模板）。
    "bonus_preview:preview_bonus_impact": (
        "非 row-scoped（薪資獎金預覽，以員工/薪資為單位）；借用 STUDENTS_WRITE "
        "gate，TODO 正名 SALARY_WRITE"
    ),
    "bonus_preview:get_bonus_dashboard": (
        "非 row-scoped（薪資獎金儀表板彙總）；借用 STUDENTS_READ gate，"
        "TODO 正名 SALARY_READ"
    ),
    # 選項端點：回傳常數選項清單（reason / type enum），不含任何學生資料。
    "student_change_logs:get_change_log_options": "回傳常數選項清單，無學生 PII",
    "student_communications:get_options": "回傳常數選項清單，無學生 PII",
    # 聯絡簿範本庫：以 owner_user_id / shared scope 過濾，非學生 row-scoped。
    # list 有 owner filter、create 的 owner 由建立者決定（無 IDOR）。
    "contact_book_templates:list_templates": (
        "範本庫，owner_user_id/shared scope（非 student row-scoped）"
    ),
    "contact_book_templates:create_template": (
        "範本庫，建立者即 owner（非 student row-scoped）"
    ),
}

# ---------------------------------------------------------------------------
# Baseline 快照：目前已知「未套標準 scope 過濾」但這一輪未修補的端點。
# 守衛只報「不在此清單也不在 EXEMPT」的端點 → 凍結現狀 + 偵測新增漂移。
# 修補一個就從此移除（守衛會在 lint_api_dir 偵測 stale 條目並報錯，逼迫清理）。
# 每筆標分類，作為後續 sprint 的明文待辦——不是「已驗證安全」。
# ---------------------------------------------------------------------------
KNOWN_UNSCOPED: dict[str, str] = {
    # 逐筆 / 跨筆學生資料但無 access 檢查 → latent fail-open（自訂
    # STUDENTS_*:own_class 角色可越權）。標準角色無 STUDENTS_* 故目前無 active 越權。
    "students:get_student_records_timeline": (
        "PER_ROW_TODO: student_id 可選，None 時跨學生；需 assert_student_access "
        "或 student_ids_in_scope（涉及 list_timeline service 簽名，故未在本輪修）"
    ),
    "students:create_student": (
        "WRITE_SCOPE_TODO: 建立學生的班級 scope 語意待業務確認"
        "（自訂 STUDENTS_WRITE:own_class 角色可建到任意班）"
    ),
    "students:graduate_student": (
        "PER_ROW_TODO: 單筆 lifecycle write 無 assert_student_access"
    ),
    "students:transition_student_lifecycle": (
        "PER_ROW_TODO: 單筆 lifecycle write 無 assert_student_access"
    ),
    # 範本庫 ownership IDOR（非 student-scope，另案）：只 filter(id==template_id)，
    # 缺 owner_user_id 檢查 → 持 PORTFOLIO_WRITE 者可改/刪/promote 他人 personal 範本。
    "contact_book_templates:update_template": (
        "OWNER_IDOR_TODO: 缺 owner_user_id 檢查（非 student-scope）"
    ),
    "contact_book_templates:delete_template": (
        "OWNER_IDOR_TODO: 缺 owner_user_id 檢查（非 student-scope）"
    ),
    "contact_book_templates:promote_to_shared": (
        "OWNER_IDOR_TODO: 缺 owner_user_id 檢查（非 student-scope）"
    ),
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _is_endpoint(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """函式是否為 FastAPI 端點（有 @router.<verb>(...) decorator）。"""
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in _HTTP_METHODS:
            return True
    return False


def _gate_perms(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """從函式參數 default（``= Depends(require_*_permission(Permission.X))``）
    抽出 gate 使用的權限 code 集合。"""
    perms: set[str] = set()
    defaults = list(func.args.defaults) + list(func.args.kw_defaults)
    for d in defaults:
        if d is None:
            continue
        for call in ast.walk(d):
            if not isinstance(call, ast.Call):
                continue
            fname = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else (call.func.id if isinstance(call.func, ast.Name) else None)
            )
            if fname not in GATE_FUNCS:
                continue
            for arg in call.args:
                # Permission.STUDENTS_READ  → Attribute(attr='STUDENTS_READ')
                if isinstance(arg, ast.Attribute):
                    perms.add(arg.attr)
                # 字串字面量 "STUDENTS_READ"
                elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    perms.add(arg.value)
    return perms


def _names_used(node: ast.AST) -> set[str]:
    """遞迴收集 node 內所有被引用的識別字（Name.id + Attribute.attr + 被呼叫的
    函式名）。用來判斷端點是否引用了 scope helper 或同檔 helper。"""
    used: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            used.add(n.attr)
    return used


def _all_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


# ---------------------------------------------------------------------------
# Core lint
# ---------------------------------------------------------------------------


def lint_source(source: str, module_stem: str) -> list[tuple[str, str]]:
    """檢查單一 module 原始碼，回傳 (endpoint_key, message) 違規清單（空 = 合規）。

    已排除 EXEMPT（永久合規豁免）；**未**排除 KNOWN_UNSCOPED baseline——
    baseline 分流由 partition_violations() 在 api-dir 層級處理（含 stale 偵測）。

    判定一個 scope-aware-gated 端點為合規的條件（任一）：
      1. 端點 body 直接引用 scope helper（SCOPE_HELPER_NAMES）。
      2. 端點呼叫了同檔中「（遞迴）最終引用 scope helper」的私有 function
         （多層間接，涵蓋 `endpoint → _scoped_query → _student_ids_in_scope
         → student_ids_in_scope` 這類 delegate wrapper 鏈）。
      3. 端點在 EXEMPT 清單（非 row-scoped 資源）。
    否則視為潛在 fail-open 違規。
    """
    tree = ast.parse(source)
    funcs = _all_functions(tree)

    # 同檔中「（遞迴）最終會走到 scope helper」的 function 名集合。
    # init：body 直接引用葉子 helper 者；fixpoint：呼叫了 carrying function 者
    # 亦標記為 carrying，迭代至收斂——涵蓋任意深度的 delegate wrapper 鏈。
    used_by_func = {f.name: _names_used(f) for f in funcs}
    helper_carrying_funcs = {
        name for name, used in used_by_func.items() if used & SCOPE_HELPER_NAMES
    }
    changed = True
    while changed:
        changed = False
        for name, used in used_by_func.items():
            if name in helper_carrying_funcs:
                continue
            if used & helper_carrying_funcs:
                helper_carrying_funcs.add(name)
                changed = True

    violations: list[tuple[str, str]] = []
    for func in funcs:
        if not _is_endpoint(func):
            continue
        aware = _gate_perms(func) & SCOPE_AWARE_PERMS
        if not aware:
            continue

        key = f"{module_stem}:{func.name}"
        if key in EXEMPT:
            continue

        used = _names_used(func)
        if used & SCOPE_HELPER_NAMES:
            continue
        if used & helper_carrying_funcs:
            continue

        violations.append(
            (
                key,
                f"{key} (line {func.lineno}) gate 用 scope-aware 權限 "
                f"{sorted(aware)} 但未套任何 scope 過濾",
            )
        )
    return violations


def lint_file(path: Path) -> list[tuple[str, str]]:
    return lint_source(path.read_text(encoding="utf-8"), path.stem)


def lint_api_dir(api_root: Path) -> list[tuple[str, str]]:
    """掃 api/ 下所有 .py，回傳全部 (key, message) 違規（已排 EXEMPT）。"""
    all_violations: list[tuple[str, str]] = []
    for py in sorted(api_root.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        all_violations.extend(lint_file(py))
    return all_violations


def partition_violations(
    violations: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """把違規分流為 (active, stale_baseline)。

    - active：不在 KNOWN_UNSCOPED baseline 的違規 message → 守衛應 fail 的項目
      （新增的、未登記的潛在 fail-open）。
    - stale_baseline：登記在 KNOWN_UNSCOPED 但實際已不再違規的 key → 表示該端點
      已被修補，baseline 條目過時，應移除（守衛亦 fail，逼迫清理，避免 baseline 腐爛）。
    """
    seen_keys = {key for key, _ in violations}
    active = [msg for key, msg in violations if key not in KNOWN_UNSCOPED]
    stale = [key for key in KNOWN_UNSCOPED if key not in seen_keys]
    return active, stale


def _default_api_root() -> Path:
    return Path(__file__).resolve().parent.parent / "api"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    api_root = Path(argv[0]) if argv else _default_api_root()
    active, stale = partition_violations(lint_api_dir(api_root))

    if active:
        print(
            f"❌ scope-filter 守衛發現 {len(active)} 個新的潛在 fail-open 端點：\n",
            file=sys.stderr,
        )
        for msg in active:
            print(f"  - {msg}", file=sys.stderr)
        print(
            "\n修法：套 utils.portfolio_access 的 scope helper（per-row 過濾），"
            "或對全園彙總端點用 assert_all_scope(...) 鎖 :all；"
            "若資源非 student row-scoped，加進 EXEMPT 並附原因。",
            file=sys.stderr,
        )
    if stale:
        print(
            f"\n⚠️  {len(stale)} 個 KNOWN_UNSCOPED baseline 條目已不再違規"
            "（端點已修補），請從 KNOWN_UNSCOPED 移除：\n",
            file=sys.stderr,
        )
        for key in stale:
            print(f"  - {key}", file=sys.stderr)

    if active or stale:
        return 1

    n_known = len(KNOWN_UNSCOPED)
    print(
        "✅ scope-filter 守衛通過：無新增未過濾端點。"
        f"（{n_known} 個 KNOWN_UNSCOPED baseline 待後續 sprint 處理）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
