"""tests/consent/test_consent_chokepoint_coverage.py — chokepoint coverage 動態掃描（RA-MED-4 防回歸）。

舊版：靜態白名單 7 檔，只能偵測「移除既有 gate」，無法偵測「新增繞過咽喉的上傳路徑」。

新版（P2-1 review P1-2）：
  - 動態掃描 api/ + services/ 所有含上傳呼叫的 .py 檔
  - 對每個這類檔，斷言要麼接了咽喉，要麼在豁免白名單中（附原因）
  - 新增不在白名單、又沒接咽喉的上傳檔 → 測試 fail
  - 保留舊版 2 個測試以維持回歸覆蓋

上傳模式（scan patterns）：
  - `put_attachment(`   ← storage wrapper
  - `get_backend().save(` ← 直接 backend API
  - `backend.save(`    ← 直接 backend API（本地 backend 變數名）

咽喉 patterns（任一即視為接咽喉）：
  - `enforce_student_cross_border`  ← endpoint 上傳守門員
  - `consent_check_student_scope`   ← background job 用

豁免白名單（KNOWN_NON_PII_OR_DEFERRED）：
  儲存相對路徑（相對於 repo root），附原因說明。
  非學生 PII 或 plan 明確 defer 的路徑可加入此清單。
  新增時必須附原因，由 reviewer 確認。
"""

import pathlib
import re

# ── 靜態白名單（舊版）保留供直接回歸斷言 ───────────────────────────────────

# 6 個 endpoint：用 enforce_student_cross_border 守門
ENDPOINT_UPLOAD_FILES = {
    "api/portal/contact_book.py",
    "api/parent_portal/medications.py",
    "api/parent_portal/events.py",
    "api/parent_portal/leaves.py",
    "api/parent_portal/messages.py",
    "api/portal/parent_messages.py",
}

# 1 個 background job：用 consent_check_student_scope 判定
BACKGROUND_UPLOAD_FILES = {"api/portfolio/reports.py"}

# ── 動態掃描常數 ─────────────────────────────────────────────────────────────

# 上傳呼叫 patterns（re.search 用）
_UPLOAD_PATTERNS = [
    r"put_attachment\(",
    r"get_backend\(\)\.save\(",
    r"backend\.save\(",
]

# 咽喉 patterns（任一 substring 存在即視為已接咽喉）
_GATE_SUBSTRINGS = [
    "enforce_student_cross_border",
    "consent_check_student_scope",
]

# 豁免白名單（相對 repo root 路徑）：非學生 PII 或 plan 明確 defer
# 格式：路徑 → 豁免原因（供審計追蹤）
KNOWN_NON_PII_OR_DEFERRED: dict[str, str] = {
    "api/vendor_payments.py": "廠商付款附件（供應商文件）：無學生 PII，豁免 cross_border 咽喉",
    "api/announcements.py": "公告附件：系統公告非學生個資，豁免 cross_border 咽喉",
    "api/attachments.py": "管理端通用附件：非學生 PII 上傳（admin 行政文件），豁免 cross_border 咽喉",
    "api/attendance/upload.py": "打卡/出勤 CSV 匯入：儲存匯入原始檔（員工資料），非學生 PII，豁免",
    "api/portal/leaves.py": "教師端請假附件：員工自身請假文件，非學生 PII，豁免",
    "api/activity/settings.py": "才藝課程海報圖片：行銷素材，無學生 PII，豁免",
}


def _root() -> pathlib.Path:
    """回傳 ivy-backend repo root。

    此檔位於 tests/consent/test_consent_chokepoint_coverage.py：
      parents[0] = tests/consent/
      parents[1] = tests/
      parents[2] = ivy-backend repo root（含 api/ 目錄）
    """
    return pathlib.Path(__file__).resolve().parents[2]


def _has_upload_call(src: str) -> bool:
    """檔案內容是否含上傳呼叫 pattern。"""
    return any(re.search(p, src) for p in _UPLOAD_PATTERNS)


def _has_gate(src: str) -> bool:
    """檔案內容是否含咽喉 pattern。"""
    return any(sub in src for sub in _GATE_SUBSTRINGS)


# ── 舊版靜態回歸測試（保留）────────────────────────────────────────────────


def test_endpoint_upload_sites_have_cross_border_gate():
    """6 個含學生 PII 的 endpoint 上傳路徑均接了 enforce_student_cross_border 咽喉。"""
    root = _root()
    for rel in sorted(ENDPOINT_UPLOAD_FILES):
        src = (root / rel).read_text(encoding="utf-8")
        assert (
            "enforce_student_cross_border" in src
        ), f"{rel} 含學生 PII 上傳但未接 cross_border 咽喉（RA-MED-4 防回歸）"


def test_background_upload_sites_have_cross_border_gate():
    """background job 的學生 PII 上傳路徑接了 consent_check_student_scope 判定。"""
    root = _root()
    for rel in sorted(BACKGROUND_UPLOAD_FILES):
        src = (root / rel).read_text(encoding="utf-8")
        assert (
            "consent_check_student_scope" in src
        ), f"{rel} background job 未接 cross_border 判定（RA-MED-4 防回歸）"


# ── 動態掃描測試（P2-1 review P1-2）────────────────────────────────────────


def test_dynamic_upload_sites_all_gated_or_exempted():
    """所有含上傳呼叫的 .py 檔，要麼接了咽喉，要麼在豁免白名單中。

    新增含學生 PII 的上傳路徑卻未接咽喉，此測試即 fail（防回歸）。
    新增確認非學生 PII 的路徑，須加入 KNOWN_NON_PII_OR_DEFERRED 並附原因。

    保護範圍：api/ + services/ 所有 .py 檔。
    """
    root = _root()

    # 掃描 api/ + services/ 下所有 .py
    scan_dirs = [root / "api", root / "services"]

    upload_files: list[pathlib.Path] = []
    for d in scan_dirs:
        upload_files.extend(
            p
            for p in d.rglob("*.py")
            if _has_upload_call(p.read_text(encoding="utf-8"))
        )

    # 確認掃描有命中已知檔案（防 glob 路徑錯誤導致 vacuous pass）
    rel_set = {str(p.relative_to(root)) for p in upload_files}
    assert "api/parent_portal/medications.py" in rel_set, (
        "掃描應命中 api/parent_portal/medications.py，"
        "請確認掃描路徑是否正確（根目錄是否為 ivy-backend repo root）"
    )

    violations: list[str] = []
    for fpath in sorted(upload_files):
        rel = str(fpath.relative_to(root))
        src = fpath.read_text(encoding="utf-8")

        if _has_gate(src):
            continue  # 已接咽喉，通過

        if rel in KNOWN_NON_PII_OR_DEFERRED:
            continue  # 在豁免白名單，通過

        violations.append(rel)

    assert not violations, (
        "以下上傳路徑未接 cross_border 咽喉，也不在豁免白名單中。\n"
        "若為學生 PII 上傳路徑，請加入 enforce_student_cross_border 或 consent_check_student_scope。\n"
        "若確認非學生 PII（例如員工文件、系統公告），請加入 KNOWN_NON_PII_OR_DEFERRED 並附原因。\n\n"
        "違規路徑（新增上傳路徑須接 cross_border 咽喉或加入豁免白名單）：\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
