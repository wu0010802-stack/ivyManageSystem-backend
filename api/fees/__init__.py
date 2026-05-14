"""api/fees — 學費/費用管理 API（拆 sub-package 後的 aggregate router）

原 1797 行 `api/fees.py` 已依資源粒度拆為：
- `templates.py`：費用範本 CRUD
- `generation.py`：批次產生 FeeRecord（範本驅動 + 舊單 fee_item 入口）
- `records.py`：FeeItem CRUD + 學期清單 + 學費紀錄查詢/繳費/摘要
- `refunds.py`：退款建議 / 退款 / 退款歷史

子 router 各自定義 `router = APIRouter()`（無 prefix），由本檔 aggregate 到
`/api/fees` 前綴。同名 `router` 變數仍由 `from api.fees import router as fees_router`
取得，main.py 與 tests 不需改動。

為了相容既有測試（多處 `from api.fees import _apply_fee_record_filters`、
`MAX_FEE_AMOUNT`、`FEE_PAYMENT_APPROVAL_THRESHOLD` 等 import），本檔同步
re-export 子模組內的共用常數與 helper。
"""

from fastapi import APIRouter

# Re-export 共用常數 / schema / helper（保留既有 import surface）
# 順序：先 _helpers 內的公共介面 → 各子模組的 router
from ._helpers import (  # noqa: F401
    FEE_PAYMENT_APPROVAL_THRESHOLD,
    MAX_FEE_AMOUNT,
    FeeItemCreate,
    FeeItemUpdate,
    FeeTemplateCreate,
    FeeTemplateUpdate,
    GenerateFromTemplatesRequest,
    GenerateRequest,
    PayRequest,
    RefundRequest,
    RefundSuggestRequest,
    _apply_fee_record_filters,
    _invalidate_finance_summary_cache,
)

from .generation import router as _generation_router
from .records import router as _records_router
from .refunds import router as _refunds_router
from .templates import router as _templates_router

# 與舊檔同 prefix / tags，main.py 直接 `app.include_router(fees_router)` 即可
router = APIRouter(prefix="/api/fees", tags=["fees"])

router.include_router(_templates_router)
router.include_router(_generation_router)
router.include_router(_records_router)
router.include_router(_refunds_router)
