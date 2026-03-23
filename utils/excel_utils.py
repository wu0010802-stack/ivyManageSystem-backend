"""
共用 Excel 工具函式，供各 API router 使用。
"""

from io import BytesIO
from urllib.parse import quote

from fastapi.responses import StreamingResponse


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
