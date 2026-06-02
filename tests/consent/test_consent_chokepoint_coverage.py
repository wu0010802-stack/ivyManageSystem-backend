"""tests/consent/test_consent_chokepoint_coverage.py — chokepoint coverage 斷言測試（RA-MED-4 防回歸）。

枚舉 7 個含學生 PII 的上傳路徑：
  - 6 個 endpoint 路徑：必須含 enforce_student_cross_border
  - 1 個 background job 路徑：必須含 consent_check_student_scope

任何人新增含學生 PII 的上傳路徑卻未接咽喉，此測試即 fail（防回歸）。
"""

import pathlib

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


def _root() -> pathlib.Path:
    """回傳 ivy-backend repo root。

    此檔位於 tests/consent/test_consent_chokepoint_coverage.py：
      parents[0] = tests/consent/
      parents[1] = tests/
      parents[2] = ivy-backend repo root（含 api/ 目錄）
    """
    return pathlib.Path(__file__).resolve().parents[2]


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
