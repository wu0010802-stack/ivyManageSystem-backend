"""
廠商付款簽收 router（園務行政）

兩階段流程：
- POST /api/vendor-payments               建立 status='pending'
- POST /api/vendor-payments/{id}/sign     上傳簽名/照片 → status='signed'
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from datetime import date, datetime
from utils.taipei_time import now_taipei_naive
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from models.database import Employee, VendorPayment, get_session
from schemas._common import DeleteResultOut, MutationResultOut
from schemas.vendor_payments import (
    VendorPaymentAttachmentMetaOut,
    VendorPaymentListOut,
    VendorPaymentOut,
)
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.file_upload import (
    read_upload_with_size_check,
    safe_attachment_filename,
    validate_file_signature,
)
from utils.permissions import Permission
from utils.portfolio_storage import get_portfolio_storage
from utils.finance_cache import invalidate_finance_summary_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["vendor-payments"])


# ─── 常數 ────────────────────────────────────────────────────────────────
PAYMENT_METHODS = ("cash", "bank_transfer", "check", "linepay", "other")
ATTACHMENT_ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_ATTACHMENTS_PER_PAYMENT = 5
SIGNATURE_MAX_BYTES = 1 * 1024 * 1024  # 1 MB
SIGNATURE_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


# ─── Pydantic schemas ────────────────────────────────────────────────────
class VendorPaymentBase(BaseModel):
    payment_date: date
    vendor_name: str = Field(..., min_length=1, max_length=120)
    amount: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)
    payment_method: Literal["cash", "bank_transfer", "check", "linepay", "other"]
    description: Optional[str] = Field(None, max_length=255)
    invoice_number: Optional[str] = Field(None, max_length=60)
    notes: Optional[str] = None

    @field_validator("vendor_name", mode="before")
    @classmethod
    def strip_vendor(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", "invoice_number", "notes", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class VendorPaymentCreate(VendorPaymentBase):
    pass


class VendorPaymentUpdate(BaseModel):
    payment_date: Optional[date] = None
    vendor_name: Optional[str] = Field(None, min_length=1, max_length=120)
    amount: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)
    payment_method: Optional[
        Literal["cash", "bank_transfer", "check", "linepay", "other"]
    ] = None
    description: Optional[str] = Field(None, max_length=255)
    invoice_number: Optional[str] = Field(None, max_length=60)
    notes: Optional[str] = None

    @field_validator("vendor_name", mode="before")
    @classmethod
    def strip_vendor(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", "invoice_number", "notes", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class VendorPaymentSignRequest(BaseModel):
    signature_kind: Literal["drawn", "photo"]
    # 接受 data URL (e.g. "data:image/png;base64,iVBOR...") 或純 base64
    signature_data: str = Field(..., min_length=20)


# ─── Helpers ─────────────────────────────────────────────────────────────
def _employee_name(emp: Optional[Employee]) -> Optional[str]:
    if emp is None:
        return None
    return getattr(emp, "name", None)


def _to_dict(row: VendorPayment) -> dict:
    return {
        "id": row.id,
        "payment_date": row.payment_date.isoformat() if row.payment_date else None,
        "vendor_name": row.vendor_name,
        "amount": float(row.amount) if row.amount is not None else None,
        "payment_method": row.payment_method,
        "description": row.description,
        "invoice_number": row.invoice_number,
        "notes": row.notes,
        "attachments": row.attachments or [],
        "status": row.status,
        "signer_id": row.signer_id,
        "signer_name": _employee_name(row.signer),
        "signed_at": row.signed_at.isoformat() if row.signed_at else None,
        "signature_kind": row.signature_kind,
        "has_signature": bool(row.signature_key),
        "created_by_id": row.created_by_id,
        "created_by_name": _employee_name(row.created_by),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _parse_signature_payload(data: str) -> tuple[bytes, str]:
    """把 dataURL 或純 base64 解析成 (bytes, ext)。"""
    payload = data
    mime: Optional[str] = None
    if payload.startswith("data:"):
        try:
            header, payload = payload.split(",", 1)
            # header 形如 "data:image/png;base64"
            if ";base64" not in header:
                raise ValueError
            mime = header.split(":", 1)[1].split(";", 1)[0].strip().lower()
        except ValueError:
            raise HTTPException(status_code=400, detail="無效的簽名 data URL")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="簽名 base64 解碼失敗")

    if len(raw) > SIGNATURE_MAX_BYTES:
        raise HTTPException(status_code=400, detail="簽名圖檔超過 1MB 限制")
    if len(raw) < 100:
        raise HTTPException(status_code=400, detail="簽名圖檔內容過短")

    # 推測副檔名（優先 mime，回 fallback 為 png）
    ext = ".png"
    if mime == "image/jpeg":
        ext = ".jpg"
    elif mime == "image/webp":
        ext = ".webp"
    elif mime == "image/png":
        ext = ".png"

    if ext not in SIGNATURE_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"不支援的簽名格式：{mime}")

    validate_file_signature(raw, ext)
    return raw, ext


def _invalidate_finance_cache() -> None:
    """金流變動影響 /finance-summary 與 /monthly-pnl 報表快取（皆 TTL 30 分），同步失效。

    走中央 helper `utils.finance_cache.invalidate_finance_summary_cache()` 一次
    清掉兩個 category，避免新加 category 時遺漏單一呼叫站點。
    """
    invalidate_finance_summary_cache()


def _load_payment(session, payment_id: int) -> VendorPayment:
    row = (
        session.query(VendorPayment)
        .options(joinedload(VendorPayment.signer), joinedload(VendorPayment.created_by))
        .filter(VendorPayment.id == payment_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="付款紀錄不存在")
    return row


# ─── Endpoints ───────────────────────────────────────────────────────────
@router.get("/vendor-payments", response_model=VendorPaymentListOut)
def list_vendor_payments(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    vendor_name: Optional[str] = Query(None, max_length=120),
    status: Optional[Literal["pending", "signed"]] = Query(None),
    payment_method: Optional[
        Literal["cash", "bank_transfer", "check", "linepay", "other"]
    ] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_READ)
    ),
):
    session = get_session()
    try:
        q = session.query(VendorPayment).options(
            joinedload(VendorPayment.signer), joinedload(VendorPayment.created_by)
        )
        filters = []
        if start_date:
            filters.append(VendorPayment.payment_date >= start_date)
        if end_date:
            filters.append(VendorPayment.payment_date <= end_date)
        if vendor_name:
            filters.append(VendorPayment.vendor_name.ilike(f"%{vendor_name.strip()}%"))
        if status:
            filters.append(VendorPayment.status == status)
        if payment_method:
            filters.append(VendorPayment.payment_method == payment_method)
        if filters:
            q = q.filter(and_(*filters))

        total = q.count()
        rows = (
            q.order_by(VendorPayment.payment_date.desc(), VendorPayment.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "items": [_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="列表查詢失敗")
    finally:
        session.close()


@router.get("/vendor-payments/{payment_id}", response_model=VendorPaymentOut)
def get_vendor_payment(
    payment_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_READ)
    ),
):
    session = get_session()
    try:
        return _to_dict(_load_payment(session, payment_id))
    finally:
        session.close()


@router.post("/vendor-payments", status_code=201, response_model=MutationResultOut)
def create_vendor_payment(
    payload: VendorPaymentCreate,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    session = get_session()
    try:
        row = VendorPayment(
            payment_date=payload.payment_date,
            vendor_name=payload.vendor_name,
            amount=payload.amount,
            payment_method=payload.payment_method,
            description=payload.description,
            invoice_number=payload.invoice_number,
            notes=payload.notes,
            attachments=[],
            status="pending",
            created_by_id=current_user.get("user_id"),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        _invalidate_finance_cache()
        return {"message": "建立成功", "id": row.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("建立廠商付款失敗")
        raise_safe_500(e, context="建立失敗")
    finally:
        session.close()


@router.put("/vendor-payments/{payment_id}", response_model=DeleteResultOut)
def update_vendor_payment(
    payment_id: int,
    payload: VendorPaymentUpdate,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            setattr(row, k, v)
        session.commit()
        _invalidate_finance_cache()
        return {"message": "更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("更新廠商付款失敗")
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.delete("/vendor-payments/{payment_id}", response_model=DeleteResultOut)
def delete_vendor_payment(
    payment_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        # 同時把附件 / 簽名檔案從 storage 刪掉
        storage = get_portfolio_storage()
        keys = []
        if row.signature_key:
            keys.append(row.signature_key)
        for att in row.attachments or []:
            if isinstance(att, dict) and att.get("key"):
                keys.append(att["key"])
        session.delete(row)
        session.commit()
        _invalidate_finance_cache()
        for k in keys:
            try:
                storage.delete(k)
            except Exception:  # noqa: BLE001
                logger.warning("刪除 storage key 失敗：%s", k)
        return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("刪除廠商付款失敗")
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()


@router.post("/vendor-payments/{payment_id}/sign", response_model=DeleteResultOut)
def sign_vendor_payment(
    payment_id: int,
    payload: VendorPaymentSignRequest,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    """簽收：解析 base64 → 寫 storage → 更新狀態。pending 才能簽收。"""
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        if row.status != "pending":
            raise HTTPException(status_code=409, detail="只有待簽收狀態的紀錄可以簽收")

        raw, ext = _parse_signature_payload(payload.signature_data)
        storage = get_portfolio_storage()
        stored = storage.put_attachment(raw, ext)

        row.signature_kind = payload.signature_kind
        row.signature_key = stored.storage_key
        row.signer_id = current_user.get("user_id")
        row.signed_at = now_taipei_naive()
        row.status = "signed"
        session.commit()

        # audit 自動由 middleware 落 CREATE（POST），entity_id 取 URL 尾數 ID
        request.state.audit_entity_id = str(payment_id)
        request.state.audit_summary = f"簽收廠商付款 #{payment_id}"
        return {"message": "簽收成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("簽收失敗")
        raise_safe_500(e, context="簽收失敗")
    finally:
        session.close()


@router.get("/vendor-payments/{payment_id}/signature")
def get_signature_image(
    payment_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_READ)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        if not row.signature_key:
            raise HTTPException(status_code=404, detail="尚未簽收")
        storage = get_portfolio_storage()
        path = storage.absolute_path(row.signature_key)
        if not path.exists():
            raise HTTPException(status_code=404, detail="簽名檔遺失")
        ext = os.path.splitext(row.signature_key)[1].lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        return FileResponse(path=str(path), media_type=mime)
    finally:
        session.close()


@router.post("/vendor-payments/{payment_id}/attachments", status_code=201, response_model=VendorPaymentAttachmentMetaOut)
async def upload_attachment(
    payment_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        existing = list(row.attachments or [])
        if len(existing) >= MAX_ATTACHMENTS_PER_PAYMENT:
            raise HTTPException(
                status_code=400,
                detail=f"附件數量已達上限（{MAX_ATTACHMENTS_PER_PAYMENT}）",
            )

        filename = file.filename or ""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ATTACHMENT_ALLOWED_EXT:
            raise HTTPException(
                status_code=400,
                detail=f"不支援的附件格式：{ext or '未知'}；僅接受 PDF/PNG/JPG/WEBP",
            )

        content = await read_upload_with_size_check(file, extension=ext)
        validate_file_signature(content, ext)

        storage = get_portfolio_storage()
        stored = storage.put_attachment(content, ext)

        meta = {
            "key": stored.storage_key,
            "filename": safe_attachment_filename(filename, ext),
            "size": len(content),
            "mime_type": stored.mime_type,
            "uploaded_at": now_taipei_naive().isoformat(),
            "uploaded_by_id": current_user.get("user_id"),
        }
        # JSONB 欄位以「新 list 替換」方式更新，否則 SQLAlchemy 不會偵測到 in-place mutation
        row.attachments = existing + [meta]
        session.commit()

        request.state.audit_entity_id = str(payment_id)
        request.state.audit_summary = f"上傳廠商付款附件 #{payment_id}：{filename}"
        return meta
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("上傳附件失敗")
        raise_safe_500(e, context="上傳失敗")
    finally:
        session.close()


@router.delete(
    "/vendor-payments/{payment_id}/attachments", response_model=DeleteResultOut
)
def delete_attachment_endpoint(
    payment_id: int,
    request: Request,
    key: str = Query(..., min_length=1),
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        attachments = list(row.attachments or [])
        kept = [a for a in attachments if a.get("key") != key]
        if len(kept) == len(attachments):
            raise HTTPException(status_code=404, detail="附件不存在")
        row.attachments = kept
        session.commit()

        try:
            get_portfolio_storage().delete(key)
        except Exception:  # noqa: BLE001
            logger.warning("刪除 storage key 失敗：%s", key)

        request.state.audit_entity_id = str(payment_id)
        request.state.audit_summary = f"刪除廠商付款附件 #{payment_id}"
        return {"message": "附件已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("刪除附件失敗")
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()


@router.get("/vendor-payments/{payment_id}/attachments/download")
def download_attachment(
    payment_id: int,
    key: str = Query(..., min_length=1),
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_READ)
    ),
):
    session = get_session()
    try:
        row = _load_payment(session, payment_id)
        meta = next((a for a in (row.attachments or []) if a.get("key") == key), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="附件不存在")
        storage = get_portfolio_storage()
        path = storage.absolute_path(key)
        if not path.exists():
            raise HTTPException(status_code=404, detail="檔案實體遺失")
        return FileResponse(
            path=str(path),
            media_type=meta.get("mime_type") or "application/octet-stream",
            filename=meta.get("filename"),
        )
    finally:
        session.close()
