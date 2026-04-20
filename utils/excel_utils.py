"""
共用 Excel 工具函式，供各 API router 使用。

此模組集中提供：
- `sanitize_excel_value` / `SafeWorksheet`：Excel 公式注入（Formula / DDE）防護。
- `xlsx_streaming_response`：將 openpyxl Workbook 序列化為 StreamingResponse。

所有會把使用者輸入寫入 xlsx 的 router 都應透過 `SafeWorksheet` 操作 worksheet，
避免員工姓名、備註等欄位若含 `=cmd|'/C calc'!A0` 時在財會端 Excel 被執行。
"""

from io import BytesIO
from urllib.parse import quote

from fastapi.responses import StreamingResponse

# Excel 公式觸發前綴：= + - @（試算表公式），| 可觸發 DDE 攻擊，% 可觸發部分試算表注入
_FORMULA_PREFIXES = ("=", "+", "-", "@", "|", "%")


def sanitize_excel_value(value):
    """防止 Excel 公式注入（Excel Injection / DDE 攻擊）。

    策略：
    1. 先去除開頭 Tab / CR / LF — 這些字元常被用來繞過前綴偵測
       （例如 '\\t=cmd...' 不以 '=' 開頭，可繞過只檢查第一字元的邏輯）
    2. 若清理後仍以危險前綴開頭，則在最前面加上單引號
       openpyxl 會將其儲存為純字串，Excel 開啟時不會執行公式
    """
    if not isinstance(value, str):
        return value
    clean = value.lstrip("\t\r\n")
    if clean.startswith(_FORMULA_PREFIXES):
        return "'" + clean
    return clean


# 舊名相容別名（現有 code / 測試沿用 `_sanitize_excel_value`）
_sanitize_excel_value = sanitize_excel_value


class SafeWorksheet:
    """openpyxl Worksheet 薄包裝器，所有寫入路徑自動執行公式注入清理。

    將防護掛在「worksheet 寫入」這一底層，確保即使未來新增報表功能時
    忘記呼叫輔助函式，直接使用 ws.cell() 或 ws["A1"] = value 也不會
    留下 Excel / DDE 注入風險。

    使用方式：
        wb = Workbook()
        ws = SafeWorksheet(wb.active)
        ws.title = "..."                            # 屬性存取正常代理到底層
        ws.cell(row=1, column=1, value=user_input)  # 自動清理
        ws["A1"] = user_input                       # 自動清理

    設計說明：
    - .cell()    → 清理後再寫入底層，回傳真實 Cell（供設定 font/border 等）
    - __setitem__ → ws["A1"] = v 語法，清理後寫入底層 Cell.value
    - __getitem__ → ws["A1"] 語法，直接回傳底層真實 Cell（供讀值、設定樣式）
    - __getattr__ → 其餘屬性 / 方法（title、merge_cells、columns 等）透明代理
    - __setattr__ → title 等屬性設定代理到底層，'_ws' 保留給自身
    """

    def __init__(self, ws):
        object.__setattr__(self, "_ws", ws)

    def cell(self, row, column, value=None):
        return self._ws.cell(row=row, column=column, value=sanitize_excel_value(value))

    def __setitem__(self, key, value):
        self._ws[key].value = sanitize_excel_value(value)

    def append(self, iterable):
        """覆寫 append()，對 row 中每個值套用 sanitize 再寫入底層。"""
        if isinstance(iterable, dict):
            cleaned = {k: sanitize_excel_value(v) for k, v in iterable.items()}
        else:
            cleaned = [sanitize_excel_value(v) for v in iterable]
        self._ws.append(cleaned)

    def __getitem__(self, key):
        return self._ws[key]

    def __getattr__(self, name):
        return getattr(self._ws, name)

    def __setattr__(self, name, value):
        if name == "_ws":
            object.__setattr__(self, name, value)
        else:
            setattr(self._ws, name, value)


def safe_ws(wb) -> SafeWorksheet:
    """回傳 wb.active 的 SafeWorksheet 包裝器。"""
    return SafeWorksheet(wb.active)


def xlsx_streaming_response(wb, filename: str) -> StreamingResponse:
    """將 openpyxl Workbook 序列化為 StreamingResponse，Content-Disposition 支援中文檔名。"""
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    encoded = quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )
