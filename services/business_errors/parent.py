"""家長端 BusinessError subclasses — 套用既有 utils/exception_handlers envelope。

每個 subclass 帶定義好的 code（來自 ErrorCode enum）、http_status、default_message。
caller 可呼叫 `BindCodeInvalid()` 用預設訊息，或 `BindCodeInvalid("自訂訊息")` 覆寫。

base class `utils.exceptions.BusinessError` 簽章為：
    BusinessError(code: str, message: str, http_status: int = 400, *, extra=None)

故 subclass 需要 wrap `__init__` 把 class-level 預設帶入 super()。
"""

from __future__ import annotations

from typing import Any

from utils.error_codes import ErrorCode
from utils.exceptions import BusinessError


class _ParentBusinessError(BusinessError):
    """Base — 家長端 BusinessError 共通父類別（純語意分類）。

    Subclass 設 class attribute `code`（ErrorCode value）、`http_status`、`default_message`。
    """

    code: str = ""
    http_status: int = 400
    default_message: str = ""

    def __init__(
        self,
        message: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            self.code,
            message or self.default_message,
            self.http_status,
            extra=extra,
        )


# ── 家長綁定流程 ────────────────────────────────────────────────────────────


class BindCodeInvalid(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_INVALID.value
    http_status = 400
    default_message = "綁定碼無效或已過期"


class BindCodeExpired(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_EXPIRED.value
    http_status = 400
    default_message = "綁定碼已過期，請重新取得"


class BindCodeAlreadyUsed(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_ALREADY_USED.value
    http_status = 409
    default_message = "此綁定碼已被使用"


# ── LIFF 認證 ───────────────────────────────────────────────────────────────


class LineBindingExpired(_ParentBusinessError):
    code = ErrorCode.LINE_BINDING_EXPIRED.value
    http_status = 401
    default_message = "您的綁定已過期，請重新登入"


class LineBindingNotFound(_ParentBusinessError):
    code = ErrorCode.LINE_BINDING_NOT_FOUND.value
    http_status = 404
    default_message = "找不到綁定資料，請重新綁定"


class LineProfileFetchFailed(_ParentBusinessError):
    code = ErrorCode.LINE_PROFILE_FETCH_FAILED.value
    http_status = 502
    default_message = "無法取得 LINE 個人資料，請稍後再試"


# ── 家長存取資源 ────────────────────────────────────────────────────────────


class StudentNotFound(_ParentBusinessError):
    code = ErrorCode.STUDENT_NOT_FOUND.value
    http_status = 404
    default_message = "找不到對應的學生資料"


class StudentNotLinkedToParent(_ParentBusinessError):
    code = ErrorCode.STUDENT_NOT_LINKED_TO_PARENT.value
    http_status = 403
    default_message = "您無權存取此學生資料"


class PortalDataUnavailable(_ParentBusinessError):
    code = ErrorCode.PORTAL_DATA_UNAVAILABLE.value
    http_status = 404
    default_message = "資料暫時無法存取"


class ContactBookNotPublished(_ParentBusinessError):
    code = ErrorCode.CONTACT_BOOK_NOT_PUBLISHED.value
    http_status = 404
    default_message = "本日聯絡簿尚未發布"


# ── 家長端通用 ──────────────────────────────────────────────────────────────


class ConsentRequired(_ParentBusinessError):
    code = ErrorCode.CONSENT_REQUIRED.value
    http_status = 403
    default_message = "請先完成同意聲明後再使用"


class DsrRequestInvalid(_ParentBusinessError):
    code = ErrorCode.DSR_REQUEST_INVALID.value
    http_status = 400
    default_message = "資料請求內容無效"


class ParentNotAuthorized(_ParentBusinessError):
    code = ErrorCode.PARENT_NOT_AUTHORIZED.value
    http_status = 403
    default_message = "您沒有權限執行此操作"
