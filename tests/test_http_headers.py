"""Content-Disposition 中文檔名 latin-1 安全性回歸（2026-06-15 運作探測 P1-1）。

Bug：薪資單逐人下載（api/salary/detail.py）與才藝點名匯出（api/activity/
  attendance.py）用 f"attachment; filename*=UTF-8''{filename}"，但 filename 含
  中文（員工姓名／常數「點名_」），未經 RFC 5987 quote → Starlette 以 latin-1
  編碼 raw header 時 UnicodeEncodeError → 對全員 100% HTTP 500。
  既有測試走不到 latin-1 raw header 路徑，照不到此 bug。
"""

from utils.http_headers import content_disposition


def test_content_disposition_cjk_is_latin1_safe():
    """含中文的檔名，header 值必須可被 latin-1 編碼（否則 Starlette 寫 header 時 500）。"""
    value = content_disposition("salary_董雅婷_2025_10.pdf")
    value.encode("latin-1")  # 不可丟 UnicodeEncodeError
    assert value.startswith("attachment; filename*=UTF-8''")
    # 「董」(U+8463) UTF-8 = E8 91 A3 → 證明確實有 RFC 5987 quote
    assert "%E8%91%A3" in value


def test_content_disposition_inline_variant():
    value = content_disposition("點名單_美術_2025-10-01.pdf", inline=True)
    value.encode("latin-1")
    assert value.startswith("inline; filename*=UTF-8''")


def test_content_disposition_ascii_filename_roundtrip():
    value = content_disposition("salary_all_2025_10.xlsx")
    value.encode("latin-1")
    assert value == "attachment; filename*=UTF-8''salary_all_2025_10.xlsx"
