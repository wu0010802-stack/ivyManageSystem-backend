"""上傳檔案共用工具：大小限制與內容讀取。"""
from fastapi import HTTPException, UploadFile

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


async def read_upload_with_size_check(file: UploadFile) -> bytes:
    """讀取上傳檔案內容，超過 10 MB 則回傳 400。"""
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="檔案超過 10MB 限制")
    return content
