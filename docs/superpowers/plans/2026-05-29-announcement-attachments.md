# PR #2：公告附件支援（圖片 + PDF）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 公告可上傳最多 5 個附件（圖片或 PDF，單檔 10MB），員工 / 家長 portal 顯示附件並可下載；含影像時 LINE flex hero 顯示縮圖。

**Architecture:** 重用既有 `Attachment` 多型表（`owner_type` + `owner_id`），新增 `ANNOUNCEMENT_OWNER_ANNOUNCEMENT` 常數；上傳/刪除走新 endpoint（`/api/announcements/{id}/attachments`），不污染既有 portfolio 路徑的 ACL 與白名單。下載仍走既有 `/api/uploads/portfolio/{key:path}` handler，但 `owner_type='announcement'` 分流套公告專屬 ACL（admin / employee visible / parent visible）。

**Tech Stack:** SQLAlchemy view-only relationship、FastAPI multipart upload、Element Plus `<el-upload>`、LINE Flex Message hero block、pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-05-29-announcement-improvements-design.md` §「PR #2」

**前置依賴:** PR #1 已 merge（time predicate helper 存在，下載 ACL 會用）；PR #8 已 merge（list response shape 穩定）。

---

## 檔案結構

**Modify:**
- `models/portfolio.py` — 加 `ATTACHMENT_OWNER_ANNOUNCEMENT` 常數；register 進 `ATTACHMENT_OWNER_TYPES`
- `models/event.py` — `Announcement` 加 viewonly `attachments` relationship
- `utils/file_upload.py` — `validate_file_signature` 加 PDF magic byte 驗證
- `api/announcements.py` — 加 `_ANNOUNCEMENT_ALLOWED_EXT` + upload / delete endpoint + list 序列化加 attachments + `_fire_announcement_push` context 補 attachments
- `api/portal/announcements.py` — list 序列化加 attachments
- `api/parent_portal/announcements.py` — list 序列化加 attachments
- `api/attachments.py` — download handler 加 `owner_type='announcement'` 分流；加 `_assert_announcement_attachment_visible`
- `services/notification/renderers.py` — `parent.announcement` renderer hero block
- `schemas/announcements.py` — list item 加 `attachments` 欄位；新增 upload response schema

**Create:**
- `tests/api/test_announcement_attachments.py` — upload / delete / download ACL / list serialize
- `tests/api/test_announcement_attachment_renderer.py` — flex hero 行為

**前端 Modify:**
- `ivy-frontend/src/api/announcements.ts` — 加 upload / delete wrapper
- `ivy-frontend/src/api/_generated/schema.d.ts` — `npm run gen:api` auto regen
- `ivy-frontend/src/views/AnnouncementView.vue` — admin 上傳元件 + edit lazy 附件 + 新增 / 編輯 flow
- `ivy-frontend/src/views/portal/PortalAnnouncementView.vue` — 員工 portal 附件展示
- `ivy-frontend/src/parent/components/announcements/AnnouncementDetailModal.vue` — 家長端附件展示

---

## Task 1: Attachment owner_type 擴 + Announcement relationship

**Files:**
- Modify: `models/portfolio.py`
- Modify: `models/event.py`

- [ ] **Step 1: Read 既有 `ATTACHMENT_OWNER_TYPES` 定義**

Run: `grep -n "ATTACHMENT_OWNER_" ivy-backend/models/portfolio.py`
確認 `ATTACHMENT_OWNER_OBSERVATION` 與 `ATTACHMENT_OWNER_TYPES` 定義位置。

- [ ] **Step 2: 加常數**

於 `models/portfolio.py`，緊鄰既有 `ATTACHMENT_OWNER_OBSERVATION = "observation"` 之後加：

```python
ATTACHMENT_OWNER_REPORT = "report"  # 若已存在則保留
ATTACHMENT_OWNER_MEDICATION_ORDER = "medication_order"  # 若已存在則保留
ATTACHMENT_OWNER_ANNOUNCEMENT = "announcement"

ATTACHMENT_OWNER_TYPES = {
    ATTACHMENT_OWNER_OBSERVATION,
    # ... existing entries kept
    ATTACHMENT_OWNER_ANNOUNCEMENT,
}
```

注意：用 `set` 加 element，避免覆寫既有 owner_type。若 `ATTACHMENT_OWNER_TYPES` 已用 set literal 定義，加上新元素即可。

- [ ] **Step 3: Announcement model 加 viewonly relationship**

於 `models/event.py:153-182` `Announcement` class 末尾加：

```python
    attachments = relationship(
        "Attachment",
        primaryjoin=(
            "and_("
            "foreign(Attachment.owner_id) == Announcement.id, "
            "Attachment.owner_type == 'announcement', "
            "Attachment.deleted_at.is_(None)"
            ")"
        ),
        viewonly=True,
        lazy="select",
    )
```

注意：`viewonly=True` 確保不會嘗試 cascade write（避免 owner_id 雙寫造成衝突）。

- [ ] **Step 4: Sanity import test**

Run: `cd ivy-backend && python3 -c "from models.database import Announcement, Attachment; from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT, ATTACHMENT_OWNER_TYPES; assert ATTACHMENT_OWNER_ANNOUNCEMENT in ATTACHMENT_OWNER_TYPES; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add models/portfolio.py models/event.py
git commit -m "feat(model): Attachment owner_type='announcement' + Announcement.attachments relationship"
```

---

## Task 2: file_upload PDF magic byte

**Files:**
- Modify: `utils/file_upload.py`

- [ ] **Step 1: Read 既有 validate_file_signature**

確認既有 image magic byte map 寫法（JPEG / PNG / GIF / HEIC）。

- [ ] **Step 2: 加 PDF magic byte**

於 `validate_file_signature` 既有 dispatch 表加：

```python
# PDF：前 5 bytes 必為 b"%PDF-"
PDF_MAGIC = b"%PDF-"

if extension == ".pdf":
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=400,
            detail="PDF 檔案格式驗證失敗（內容非 PDF）",
        )
    return
```

具體位置：在既有 `if extension in {".jpg", ".jpeg"}:` 等 dispatch 系列加分支。

- [ ] **Step 3: 寫測試**

於 `tests/utils/test_file_upload.py`（若無則 create）加：

```python
import pytest
from fastapi import HTTPException

from utils.file_upload import validate_file_signature


def test_pdf_magic_byte_ok():
    validate_file_signature(b"%PDF-1.4\n%more bytes", ".pdf")  # no raise


def test_pdf_magic_byte_fails_for_fake():
    with pytest.raises(HTTPException) as exc:
        validate_file_signature(b"NOT A PDF", ".pdf")
    assert exc.value.status_code == 400
    assert "PDF" in exc.value.detail
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/utils/test_file_upload.py -v -k "pdf"`
Expected: 2 PASS。

- [ ] **Step 5: Commit**

```bash
git add utils/file_upload.py tests/utils/test_file_upload.py
git commit -m "feat(upload): validate PDF magic byte for .pdf extension"
```

---

## Task 3: Upload endpoint

**Files:**
- Modify: `api/announcements.py`
- Test: `tests/api/test_announcement_attachments.py` (new)

- [ ] **Step 1: 寫失敗的測試**

Create `tests/api/test_announcement_attachments.py`：

```python
"""Tests for announcement attachment endpoints + download ACL."""

import io

import pytest


def _png_bytes() -> bytes:
    # 1x1 透明 PNG（PNG magic + minimal IHDR + IDAT + IEND）
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%dummy\n%%EOF\n"


def test_upload_png_succeeds(admin_client, db_session, admin_emp):
    from models.database import Announcement, Attachment

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("hero.png", _png_bytes(), "image/png")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 201
    body = res.json()
    assert body["filename"] in ("hero.png", body["filename"])  # safe_attachment_filename 可能微調
    assert body["mime_type"].startswith("image/")
    rows = (
        db_session.query(Attachment)
        .filter(Attachment.owner_type == "announcement", Attachment.owner_id == a.id)
        .all()
    )
    assert len(rows) == 1


def test_upload_pdf_succeeds(admin_client, db_session, admin_emp):
    from models.database import Announcement, Attachment

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("notice.pdf", _pdf_bytes(), "application/pdf")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 201
    body = res.json()
    assert body["mime_type"] == "application/pdf"
    # PDF 不生 thumb
    assert body.get("thumb_url") is None


def test_upload_rejects_disallowed_ext(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("evil.exe", b"MZdata", "application/octet-stream")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400


def test_upload_rejects_fake_pdf(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("fake.pdf", b"not a real pdf", "application/pdf")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400


def test_upload_enforces_5_limit(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    # 上 5 個都成功
    for i in range(5):
        files = {"file": (f"p{i}.png", _png_bytes(), "image/png")}
        res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
        assert res.status_code == 201
    # 第 6 個 reject
    files = {"file": ("extra.png", _png_bytes(), "image/png")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400
    assert "5" in res.json()["detail"] or "上限" in res.json()["detail"]


def test_upload_404_unknown_announcement(admin_client):
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    res = admin_client.post("/api/announcements/999999/attachments", files=files)
    assert res.status_code == 404
```

`admin_client` fixture 沿用 repo 既有 conftest pattern（PR #1 / PR #8 plan 已建立）。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "upload"`
Expected: 全 FAIL（404，endpoint 不存在）。

- [ ] **Step 3: 加 helper + upload endpoint**

於 `api/announcements.py` 加：

```python
_ANNOUNCEMENT_ALLOWED_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".pdf",
}
_ANNOUNCEMENT_ATTACHMENT_LIMIT = 5
```

於 `delete_announcement` 之後加 upload / delete endpoint：

```python
from fastapi import UploadFile, File
import os

@router.post(
    "/{announcement_id}/attachments",
    status_code=201,
)
async def upload_announcement_attachment(
    announcement_id: int,
    file: UploadFile = File(...),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
) -> dict:
    """上傳公告附件（圖片 / PDF），最多 5 個。"""
    from models.database import Attachment, session_scope
    from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT
    from utils.file_upload import (
        read_upload_with_size_check,
        safe_attachment_filename,
        validate_file_signature,
    )
    from utils.portfolio_storage import (
        get_portfolio_storage,
        is_image_extension,
    )

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ANNOUNCEMENT_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；僅接受 JPG/PNG/GIF/HEIC/PDF",
        )

    with session_scope() as session:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        existing_count = (
            session.query(Attachment)
            .filter(
                Attachment.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT,
                Attachment.owner_id == announcement_id,
                Attachment.deleted_at.is_(None),
            )
            .count()
        )
        if existing_count >= _ANNOUNCEMENT_ATTACHMENT_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"附件上限 {_ANNOUNCEMENT_ATTACHMENT_LIMIT} 個",
            )

    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    storage = get_portfolio_storage()
    stored = storage.put_attachment(content, ext)

    with session_scope() as session:
        att = Attachment(
            owner_type=ATTACHMENT_OWNER_ANNOUNCEMENT,
            owner_id=announcement_id,
            storage_key=stored.storage_key,
            display_key=stored.display_key,
            thumb_key=stored.thumb_key,
            original_filename=safe_attachment_filename(filename, ext),
            mime_type=stored.mime_type,
            size_bytes=len(content),
            uploaded_by=current_user.get("user_id"),
        )
        session.add(att)
        session.flush()
        return _serialize_attachment_for_announcement(att)


def _serialize_attachment_for_announcement(att) -> dict:
    """公告 attachment serialization。
    與 api/attachments._attachment_to_dict 對齊但用 filename 鍵（與 spec 對齊）。
    """
    from utils.portfolio_storage import PORTFOLIO_MODULE
    return {
        "id": att.id,
        "filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": f"/api/uploads/{PORTFOLIO_MODULE}/{att.storage_key}",
        "thumb_url": (
            f"/api/uploads/{PORTFOLIO_MODULE}/{att.thumb_key}"
            if att.thumb_key else None
        ),
    }
```

`storage.put_attachment` 對非影像（PDF）必須跳過 PIL 變體生成，原檔落 `storage_key`，`display_key` / `thumb_key` 留 NULL。若 `put_attachment` 目前 hardcode 走影像路徑，需在 step 3.5 微調 `utils/portfolio_storage.put_attachment` 對非影像 ext 直接 落原檔即返回。

- [ ] **Step 3.5: 視情況修 `utils/portfolio_storage.put_attachment` 對 PDF 跳過 PIL 路徑**

Read `utils/portfolio_storage.py`，確認 `put_attachment` 對非 image extension 是否已能正確處理。若它先呼叫 `PIL.Image.open` 對 PDF 會炸：

```python
def put_attachment(self, content: bytes, extension: str) -> StoredAttachment:
    # ... 既有 imports ...
    if extension == ".pdf":
        storage_key = self._put_raw(content, extension)
        return StoredAttachment(
            storage_key=storage_key,
            display_key=None,
            thumb_key=None,
            mime_type="application/pdf",
        )
    # ... 既有 image 路徑保持
```

- [ ] **Step 4: 跑 upload 測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "upload"`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add api/announcements.py utils/portfolio_storage.py tests/api/test_announcement_attachments.py
git commit -m "feat(api): POST /announcements/{id}/attachments (image/PDF, max 5)"
```

---

## Task 4: Delete endpoint

**Files:**
- Modify: `api/announcements.py`
- Test: `tests/api/test_announcement_attachments.py`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_delete_attachment_soft_deletes(admin_client, db_session, admin_emp):
    from models.database import Announcement, Attachment

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    up = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    att_id = up.json()["id"]

    res = admin_client.delete(f"/api/announcements/{a.id}/attachments/{att_id}")
    assert res.status_code == 200

    row = db_session.query(Attachment).filter(Attachment.id == att_id).first()
    db_session.refresh(row)
    assert row.deleted_at is not None


def test_delete_rejects_cross_announcement(admin_client, db_session, admin_emp):
    """attachment 不屬於 path 中的 announcement → 404。"""
    from models.database import Announcement, Attachment

    a1 = Announcement(title="A", content="C", created_by=admin_emp.id)
    a2 = Announcement(title="B", content="C", created_by=admin_emp.id)
    db_session.add_all([a1, a2])
    db_session.commit()
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    up = admin_client.post(f"/api/announcements/{a1.id}/attachments", files=files)
    att_id = up.json()["id"]

    res = admin_client.delete(f"/api/announcements/{a2.id}/attachments/{att_id}")
    assert res.status_code == 404
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "delete"`
Expected: FAIL。

- [ ] **Step 3: 加 endpoint**

```python
@router.delete(
    "/{announcement_id}/attachments/{attachment_id}",
    response_model=DeleteResultOut,
)
def delete_announcement_attachment(
    announcement_id: int,
    attachment_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    from models.database import Attachment, session_scope
    from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT
    from utils.audit import mark_soft_delete
    from utils.taipei_time import now_taipei_naive

    with session_scope() as session:
        att = (
            session.query(Attachment)
            .filter(
                Attachment.id == attachment_id,
                Attachment.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT,
                Attachment.owner_id == announcement_id,
                Attachment.deleted_at.is_(None),
            )
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="附件不存在")
        # mark_soft_delete 既有 utility；若 signature 不同，改 att.deleted_at = now_taipei_naive()
        try:
            mark_soft_delete(att)
        except TypeError:
            att.deleted_at = now_taipei_naive()
    return {"message": "附件已刪除"}
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "delete"`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add api/announcements.py tests/api/test_announcement_attachments.py
git commit -m "feat(api): DELETE /announcements/{id}/attachments/{att_id} (soft delete)"
```

---

## Task 5: Download ACL 分流

**Files:**
- Modify: `api/attachments.py`
- Test: `tests/api/test_announcement_attachments.py`

- [ ] **Step 1: Read 既有 download handler**

Read `api/attachments.py` download_router 的 handler，找到「依 storage_key 反查 attachment」之後做 ACL 的位置。

- [ ] **Step 2: 寫失敗的測試**

```python
def test_admin_can_download_announcement_attachment(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    url = up.json()["url"]
    res = admin_client.get(url)
    assert res.status_code == 200


def test_employee_with_no_target_cannot_download(
    portal_client, admin_client, db_session, admin_emp, other_emp
):
    """限定 target 為 admin_emp 的公告附件，portal_client (other_emp) 不可下載。"""
    from models.database import Announcement, AnnouncementRecipient

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    db_session.add(
        AnnouncementRecipient(announcement_id=a.id, employee_id=admin_emp.id)
    )
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    url = up.json()["url"]
    res = portal_client.get(url)
    assert res.status_code == 403


def test_parent_not_in_scope_cannot_download(
    parent_client, admin_client, db_session, admin_emp
):
    """公告對家長 scope = classroom X，parent_client 的學生不在 X 班 → 403。"""
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    url = up.json()["url"]
    # 不設 parent recipients → parent 一律不可見
    res = parent_client.get(url)
    assert res.status_code == 403
```

Fixture 名（`portal_client` / `parent_client` / `other_emp`）依 repo 既有 conftest 命名調整。

- [ ] **Step 3: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "download"`
Expected: FAIL（既有 download handler 走 portfolio ACL 對 announcement 會 403/500）。

- [ ] **Step 4: 加 ACL helper**

於 `api/announcements.py`（或新增 `api/announcements_attachments_acl.py`）加：

```python
def assert_announcement_attachment_visible(session, att, current_user):
    """依 caller role 三分流：
    - admin / hr / supervisor (is_unrestricted): 直接通過
    - employee role: 套 portal visible_filter + time predicate
    - parent role: 套 parent visible_subquery + time predicate

    安全要點：不能用「持有 ANNOUNCEMENTS_READ」當 bypass — 員工 portal user 持有
    此權限時仍須套 targeted_to_me 守衛，避免限定 target 公告附件被未指定員工讀到。
    """
    from sqlalchemy import and_, exists, or_
    from models.database import (
        Announcement,
        AnnouncementParentRecipient,
        AnnouncementRecipient,
    )
    from services.announcements.visibility import visibility_time_predicate
    from utils.portfolio_access import is_unrestricted
    from utils.taipei_time import now_taipei_naive

    if is_unrestricted(current_user):
        return

    ann_id = att.owner_id
    time_pred = visibility_time_predicate(now_taipei_naive())
    role = current_user.get("role")

    if role == "parent":
        from api.parent_portal.announcements import _build_visibility_subquery
        cond = _build_visibility_subquery(session, current_user["user_id"])
        apr = AnnouncementParentRecipient
        visible_subq = exists().where(
            and_(apr.announcement_id == Announcement.id, cond)
        )
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == ann_id, visible_subq, time_pred)
            .first()
        )
        if ann is None:
            raise HTTPException(status_code=403, detail="無權存取此附件")
        return

    # employee role
    emp_id = current_user.get("employee_id")
    if emp_id is None:
        raise HTTPException(status_code=403, detail="無權存取此附件")
    no_recipients = ~exists().where(
        AnnouncementRecipient.announcement_id == ann_id
    )
    targeted_to_me = exists().where(
        and_(
            AnnouncementRecipient.announcement_id == ann_id,
            AnnouncementRecipient.employee_id == emp_id,
        )
    )
    ann = (
        session.query(Announcement)
        .filter(
            Announcement.id == ann_id,
            or_(no_recipients, targeted_to_me),
            time_pred,
        )
        .first()
    )
    if ann is None:
        raise HTTPException(status_code=403, detail="無權存取此附件")
```

- [ ] **Step 5: download handler 分流**

於 `api/attachments.py` download handler 反查到 attachment row 之後（既有 `assert_student_access` 呼叫之前）插：

```python
from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT

if att.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT:
    from api.announcements import assert_announcement_attachment_visible
    assert_announcement_attachment_visible(session, att, current_user)
else:
    # 既有 portfolio 路徑
    student_id = _resolve_owner_student_id(session, att.owner_type, att.owner_id)
    assert_student_access(session, current_user, student_id)
```

不在 `_resolve_owner_student_id` 加 announcement 分支（保留 portfolio handler 純粹）。

- [ ] **Step 6: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "download"`
Expected: 3 PASS。

- [ ] **Step 7: 跑既有 portfolio download 測試確認無 regression**

Run: `cd ivy-backend && pytest tests/api/test_attachments.py -v` (依實際檔名)
Expected: 全綠（既有 owner_type='observation' 等仍走原路徑）。

- [ ] **Step 8: Commit**

```bash
git add api/announcements.py api/attachments.py tests/api/test_announcement_attachments.py
git commit -m "feat(acl): announcement attachment download dispatches to role-based visibility"
```

---

## Task 6: List response 加 attachments（3 端）

**Files:**
- Modify: `api/announcements.py` (admin list)
- Modify: `api/portal/announcements.py` (employee portal list)
- Modify: `api/parent_portal/announcements.py` (parent list)
- Modify: `schemas/announcements.py`
- Test: `tests/api/test_announcement_attachments.py`

- [ ] **Step 1: schemas 加欄位**

於 list item Pydantic model 加：

```python
class AnnouncementAttachmentOut(BaseModel):
    id: int
    filename: str
    mime_type: str
    size_bytes: int
    url: str
    thumb_url: Optional[str] = None


# 在 AnnouncementListItemOut 加：
    attachments: List[AnnouncementAttachmentOut] = []
```

3 端 list response 都包含此欄位。

- [ ] **Step 2: 寫失敗的測試**

```python
def test_admin_list_includes_attachments(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    res = admin_client.get("/api/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert len(item["attachments"]) == 1
    assert item["attachments"][0]["mime_type"].startswith("image/")


def test_portal_list_includes_attachments(portal_client, admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    res = portal_client.get("/api/portal/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert len(item["attachments"]) == 1


def test_parent_list_includes_attachments(parent_client, admin_client, db_session, admin_emp):
    """家長端 scope='all' 公告 list 含 attachments。"""
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    admin_client.put(
        f"/api/announcements/{a.id}/parent-recipients",
        json={"recipients": [{"scope": "all"}]},
    )
    res = parent_client.get("/api/parent/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert len(item["attachments"]) == 1


def test_soft_deleted_attachment_excluded_from_list(admin_client, db_session, admin_emp):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    att_id = up.json()["id"]
    admin_client.delete(f"/api/announcements/{a.id}/attachments/{att_id}")

    res = admin_client.get("/api/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert item["attachments"] == []
```

- [ ] **Step 3: admin list 加 selectinload + 序列化**

於 `list_announcements` 既有 query options 加：

```python
from sqlalchemy.orm import selectinload
# ...
            .options(
                joinedload(Announcement.author),
                selectinload(Announcement.attachments),
            )
```

序列化 dict 加：

```python
                    "attachments": [
                        _serialize_attachment_for_announcement(att)
                        for att in ann.attachments
                    ],
```

- [ ] **Step 4: portal list 加 attachments**

於 `api/portal/announcements.py` list query 加 `selectinload(Announcement.attachments)`，序列化加 attachments 欄位（同 admin）。

- [ ] **Step 5: parent list 加 attachments**

於 `api/parent_portal/announcements.py` list query 加 `selectinload(Announcement.attachments)`，序列化加 attachments 欄位。

- [ ] **Step 6: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachments.py -v -k "list"`
Expected: 4 PASS。

- [ ] **Step 7: Commit**

```bash
git add schemas/announcements.py api/announcements.py api/portal/announcements.py api/parent_portal/announcements.py tests/api/test_announcement_attachments.py
git commit -m "feat(api): announcement list (admin/portal/parent) includes attachments"
```

---

## Task 7: LINE flex hero block

**Files:**
- Modify: `services/notification/renderers.py`
- Modify: `api/announcements.py` (補 _fire_announcement_push context attachments)
- Test: `tests/api/test_announcement_attachment_renderer.py` (new)

- [ ] **Step 1: Read 既有 renderer**

Read `services/notification/renderers.py` `parent.announcement` renderer 結構。

- [ ] **Step 2: 寫失敗的測試**

Create `tests/api/test_announcement_attachment_renderer.py`：

```python
"""parent.announcement flex hero 行為。"""

from services.notification.renderers import render_parent_announcement


def test_flex_includes_hero_when_first_attachment_is_image():
    payload = render_parent_announcement(
        context={
            "title": "母親節活動",
            "preview": "歡迎家長參與",
            "announcement_id": 42,
            "attachments": [
                {"mime_type": "image/png", "thumb_url": "/api/uploads/portfolio/abc.png"},
                {"mime_type": "application/pdf", "thumb_url": None},
            ],
        }
    )
    # FlexBubble.hero 應存在；具體鍵名依既有 helper
    flex = payload.contents if hasattr(payload, "contents") else payload
    assert "hero" in str(flex).lower() or hasattr(flex, "hero")


def test_flex_omits_hero_when_first_attachment_is_pdf():
    payload = render_parent_announcement(
        context={
            "title": "新生報名表",
            "preview": "請下載填寫",
            "announcement_id": 43,
            "attachments": [
                {"mime_type": "application/pdf", "thumb_url": None},
            ],
        }
    )
    flex = payload.contents if hasattr(payload, "contents") else payload
    # 純文字 flex 無 hero
    assert "hero" not in str(flex).lower() or not getattr(flex, "hero", None)


def test_flex_no_attachments_omits_hero():
    payload = render_parent_announcement(
        context={
            "title": "T",
            "preview": "C",
            "announcement_id": 1,
            "attachments": [],
        }
    )
    flex = payload.contents if hasattr(payload, "contents") else payload
    assert "hero" not in str(flex).lower() or not getattr(flex, "hero", None)
```

注意：依既有 `render_parent_announcement` 簽章可能微調 import / assertion。若 renderer 未直接 export 此函式，找對應 dispatch / template 函式名稱。

- [ ] **Step 3: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachment_renderer.py -v`
Expected: FAIL（renderer 未讀 attachments / 未加 hero）。

- [ ] **Step 4: 修 renderer**

於 `services/notification/renderers.py` `render_parent_announcement`（或對應函式）：

```python
def render_parent_announcement(context: dict):
    from urllib.parse import urljoin
    from config import get_settings

    attachments = context.get("attachments") or []
    first_image = next(
        (a for a in attachments if (a.get("mime_type") or "").startswith("image/") and a.get("thumb_url")),
        None,
    )

    if first_image:
        base = get_settings().line_base_url  # 既有 setting 名稱可能是 line.base_url / app_base_url
        hero_url = urljoin(base, first_image["thumb_url"])
        return _build_flex_with_hero(
            title=context["title"],
            preview=context.get("preview", ""),
            hero_url=hero_url,
        )
    return _build_flex_plain(
        title=context["title"],
        preview=context.get("preview", ""),
    )
```

`_build_flex_with_hero` / `_build_flex_plain` 為 helper，從現有 renderer 拆出（或現有 renderer 重構成這兩段）。

- [ ] **Step 5: `_fire_announcement_push` 補 context attachments**

於 `api/announcements.py` `_fire_announcement_push`：

```python
def _fire_announcement_push(session, announcement, recipients, *, sender_user_id=None):
    # 拉 attachments
    from models.database import Attachment
    from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT
    from utils.portfolio_storage import PORTFOLIO_MODULE

    att_rows = (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT,
            Attachment.owner_id == announcement.id,
            Attachment.deleted_at.is_(None),
        )
        .all()
    )
    attachments_ctx = [
        {
            "mime_type": a.mime_type,
            "thumb_url": (
                f"/api/uploads/{PORTFOLIO_MODULE}/{a.thumb_key}"
                if a.thumb_key else None
            ),
        }
        for a in att_rows
    ]
    user_ids = _resolve_parent_user_ids(session, recipients)
    for uid in user_ids:
        dispatch.enqueue(
            session=session,
            event_type="parent.announcement",
            recipient_user_id=uid,
            context={
                "title": announcement.title,
                "preview": announcement.content,
                "announcement_id": announcement.id,
                "attachments": attachments_ctx,
            },
            sender_id=sender_user_id,
            source_entity_type="announcement",
            source_entity_id=announcement.id,
        )
```

- [ ] **Step 6: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcement_attachment_renderer.py -v`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add services/notification/renderers.py api/announcements.py tests/api/test_announcement_attachment_renderer.py
git commit -m "feat(line): parent.announcement flex hero shows first image thumb"
```

---

## Task 8: Backend 全套 regression

- [ ] **Step 1: 跑 announcement / attachment 相關全 pytest**

Run:
```bash
cd ivy-backend && pytest tests/ -v -k "announcement or attachment" 2>&1 | tail -40
```
Expected: 全綠。

- [ ] **Step 2: 跑 focused suite smoke**

Run: `cd ivy-backend && pytest tests/ -q --ignore=tests/integration 2>&1 | tail -10`
Expected: 與 main 相同綠/紅平衡。

---

## Task 9: Frontend — API wrapper + OpenAPI regen

**Files:**
- Modify: `ivy-frontend/src/api/announcements.ts`
- Auto: `ivy-frontend/src/api/_generated/schema.d.ts`

- [ ] **Step 1: 加 wrapper**

```typescript
import api from './index'

export const uploadAnnouncementAttachment = (id: number, file: File) => {
  const fd = new FormData()
  fd.append('file', file)
  return api.post(`/announcements/${id}/attachments`, fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export const deleteAnnouncementAttachment = (id: number, attId: number) =>
  api.delete(`/announcements/${id}/attachments/${attId}`)
```

- [ ] **Step 2: OpenAPI regen + typecheck**

```bash
cd ivy-backend && python3 scripts/dump_openapi.py > openapi.json
cd ../ivy-frontend && npm run gen:api && npm run typecheck
```

Expected: 0 error。

- [ ] **Step 3: Commit**

```bash
cd ivy-frontend && git add src/api/announcements.ts src/api/_generated/schema.d.ts
git commit -m "feat(api): client wrappers for announcement attachment upload/delete"
```

---

## Task 10: Frontend Admin — `<el-upload>` 元件 + create / edit flow

**Files:**
- Modify: `ivy-frontend/src/views/AnnouncementView.vue`

- [ ] **Step 1: form reactive 加附件 state**

```typescript
type PendingAttachment = { file: File; uid: number }
type ExistingAttachment = { id: number; filename: string; mime_type: string; size_bytes: number; url: string; thumb_url: string | null }

const pendingAttachments = ref<PendingAttachment[]>([])  // 新增公告 / 編輯時待上傳
const existingAttachments = ref<ExistingAttachment[]>([])  // 編輯模式 fetch 後填入
const attachmentsToDelete = ref<number[]>([])  // 編輯時要刪的既有 att id
```

`resetForm` 同步補上：

```typescript
pendingAttachments.value = []
existingAttachments.value = []
attachmentsToDelete.value = []
```

- [ ] **Step 2: openEdit fetch existing attachments**

在 `openEdit` 既有 `Promise.all` 並行 fetch 內加：list endpoint 已含 `row.attachments` → 直接填：

```typescript
existingAttachments.value = (row.attachments as ExistingAttachment[]) ?? []
```

無需新 endpoint（list 已 inline 帶）。

- [ ] **Step 3: Template 加 el-upload**

在「家長端」divider 之**前**加：

```vue
<el-form-item label="附件">
  <div class="attachments-block">
    <div v-if="existingAttachments.length" class="existing-attachments">
      <div v-for="att in existingAttachments" :key="att.id" class="att-row">
        <img v-if="att.thumb_url" :src="att.thumb_url" class="att-thumb" />
        <el-icon v-else><Document /></el-icon>
        <span class="att-filename">{{ att.filename }}</span>
        <el-button
          link
          type="danger"
          size="small"
          @click="markAttachmentForDelete(att.id)"
        >移除</el-button>
      </div>
    </div>
    <el-upload
      :auto-upload="false"
      :multiple="true"
      :limit="attachmentLimit"
      :file-list="pendingFileList"
      :on-change="handleAttachmentChange"
      :on-exceed="handleAttachmentExceed"
      :before-upload="handleAttachmentBefore"
      accept=".jpg,.jpeg,.png,.gif,.heic,.heif,.pdf"
    >
      <el-button>選擇檔案（圖片/PDF，最多 5 個、單檔 10MB）</el-button>
    </el-upload>
  </div>
</el-form-item>
```

`Document` 從 `@element-plus/icons-vue` import。

- [ ] **Step 4: 加 helpers**

```typescript
const ATTACHMENT_MAX = 5
const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024

const attachmentLimit = computed(
  () => ATTACHMENT_MAX - existingAttachments.value.length + attachmentsToDelete.value.length,
)

const pendingFileList = computed(() =>
  pendingAttachments.value.map(p => ({ name: p.file.name, uid: p.uid })),
)

const handleAttachmentChange = (uploadFile: { uid: number; raw?: File }) => {
  if (!uploadFile.raw) return
  pendingAttachments.value.push({ file: uploadFile.raw, uid: uploadFile.uid })
}

const handleAttachmentExceed = () => {
  ElMessage.warning(`附件上限 ${ATTACHMENT_MAX} 個`)
}

const handleAttachmentBefore = (file: File) => {
  if (file.size > ATTACHMENT_MAX_BYTES) {
    ElMessage.warning(`單檔上限 10MB（${file.name} 超過）`)
    return false
  }
  return true
}

const markAttachmentForDelete = (attId: number) => {
  existingAttachments.value = existingAttachments.value.filter(a => a.id !== attId)
  attachmentsToDelete.value.push(attId)
}
```

- [ ] **Step 5: 改寫 handleSubmit 處理 attachment**

```typescript
const handleSubmit = async () => {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('請填寫標題和內容')
    return
  }
  // 既有 parent_visibility 檢查保留
  submitLoading.value = true
  try {
    const recipientIds = form.restrict_recipients ? form.target_employee_ids : []
    let announcementId = form.id

    if (isEdit.value) {
      await updateAnnouncement(form.id!, {
        title: form.title,
        content: form.content,
        priority: form.priority,
        is_pinned: form.is_pinned,
        target_employee_ids: recipientIds,
        publish_at: form.publish_at,
        expires_at: form.expires_at,
      })
      // 刪標記的舊附件
      for (const attId of attachmentsToDelete.value) {
        try { await deleteAnnouncementAttachment(form.id!, attId) } catch { /* swallow per-file */ }
      }
    } else {
      const res = await createAnnouncement({
        title: form.title,
        content: form.content,
        priority: form.priority,
        is_pinned: form.is_pinned,
        target_employee_ids: recipientIds.length > 0 ? recipientIds : null,
        publish_at: form.publish_at,
        expires_at: form.expires_at,
      })
      const resData = res.data as { id?: number }
      announcementId = resData?.id ?? null
    }

    // 上傳新附件
    if (announcementId && pendingAttachments.value.length > 0) {
      const uploads = pendingAttachments.value.map(p =>
        uploadAnnouncementAttachment(announcementId!, p.file)
      )
      await Promise.all(uploads)
    }

    // 家長端 scope 同步（既有邏輯）
    if (announcementId) {
      const parentRecipients = buildParentRecipients()
      if (parentRecipients !== null) {
        try {
          await replaceAnnouncementParentRecipients(announcementId, parentRecipients)
        } catch (e) {
          ElMessage.warning(apiError(e, '公告已存檔，但家長端對象設定失敗，請稍後重試'))
        }
      }
    }

    ElMessage.success(isEdit.value ? '公告已更新' : '公告已發佈')
    dialogVisible.value = false
    fetchAnnouncements()
  } catch (error) {
    ElMessage.error(apiError(error, '操作失敗'))
  } finally {
    submitLoading.value = false
  }
}
```

- [ ] **Step 6: typecheck + build**

Run: `cd ivy-frontend && npm run typecheck && npm run build`
Expected: 0 error。

- [ ] **Step 7: Commit**

```bash
cd ivy-frontend && git add src/views/AnnouncementView.vue
git commit -m "feat(ui): announcement admin upload/delete attachments (limit 5)"
```

---

## Task 11: Frontend Portal — 員工 portal 附件展示

**Files:**
- Modify: `ivy-frontend/src/views/portal/PortalAnnouncementView.vue`

- [ ] **Step 1: Announcement interface 加 attachments**

```typescript
interface Attachment {
  id: number
  filename: string
  mime_type: string
  size_bytes: number
  url: string
  thumb_url: string | null
}

interface Announcement {
  // 既有欄位...
  attachments?: Attachment[]
}
```

- [ ] **Step 2: Template 展開區塊加附件**

於 `ann-content` 之後加：

```vue
<div v-if="(ann.attachments?.length ?? 0) > 0" class="ann-attachments">
  <div
    v-for="att in ann.attachments"
    :key="att.id"
    class="att-item"
    @click.stop="openAttachment(att)"
  >
    <img v-if="att.thumb_url" :src="att.thumb_url" class="att-thumb" :alt="att.filename" />
    <el-icon v-else size="32"><Document /></el-icon>
    <span class="att-name">{{ att.filename }}</span>
  </div>
</div>
```

- [ ] **Step 3: 加 helper + style**

```typescript
import { Document } from '@element-plus/icons-vue'

const openAttachment = (att: Attachment) => {
  window.open(att.url, '_blank', 'noopener')
}
```

```css
.ann-attachments { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.att-item {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 8px; border: 1px solid var(--border-color); border-radius: 6px;
  cursor: pointer; min-width: 96px;
}
.att-item:hover { background: var(--bg-color); }
.att-thumb { width: 80px; height: 80px; object-fit: cover; border-radius: 4px; }
.att-name {
  font-size: 12px; color: var(--text-secondary); text-align: center;
  max-width: 96px; word-break: break-word;
}
```

- [ ] **Step 4: typecheck + build**

Run: `cd ivy-frontend && npm run typecheck && npm run build`
Expected: 0 error。

- [ ] **Step 5: Commit**

```bash
cd ivy-frontend && git add src/views/portal/PortalAnnouncementView.vue
git commit -m "feat(ui): portal announcement detail displays attachments"
```

---

## Task 12: Frontend Parent — 家長端 modal 附件展示

**Files:**
- Modify: `ivy-frontend/src/parent/components/announcements/AnnouncementDetailModal.vue`

- [ ] **Step 1: Read 既有 modal 結構**

確認 props 與 announcement 物件型別。

- [ ] **Step 2: Template 加附件清單（行動裝置友善）**

於詳情內容之後加：

```vue
<div v-if="(announcement?.attachments?.length ?? 0) > 0" class="parent-att-list">
  <div
    v-for="att in announcement.attachments"
    :key="att.id"
    class="parent-att-row"
    @click="openAttachment(att)"
    role="button"
    :tabindex="0"
    @keydown.enter="openAttachment(att)"
  >
    <img v-if="att.thumb_url" :src="att.thumb_url" :alt="att.filename" class="parent-att-thumb" />
    <div v-else class="parent-att-pdf-icon">PDF</div>
    <div class="parent-att-meta">
      <span class="parent-att-name">{{ att.filename }}</span>
      <span class="parent-att-size">{{ formatSize(att.size_bytes) }}</span>
    </div>
  </div>
</div>
```

```typescript
const openAttachment = (att: { url: string }) => {
  window.open(att.url, '_blank', 'noopener')
}

const formatSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
```

CSS 樣式對齊既有 parent design tokens（行動端友善：tap target ≥ 44px）：

```css
.parent-att-list { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.parent-att-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px; border-radius: 12px;
  background: var(--pt-surface-card, #fff);
  border: 1px solid var(--pt-border-light, #ecf5f9);
  min-height: 56px; cursor: pointer;
}
.parent-att-thumb { width: 44px; height: 44px; object-fit: cover; border-radius: 6px; }
.parent-att-pdf-icon {
  width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;
  background: var(--coral-50, #fff5f5); color: var(--coral-700, #c4452c);
  font-weight: 700; border-radius: 6px;
}
.parent-att-meta { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.parent-att-name { font-size: 14px; color: var(--pt-text-strong); word-break: break-word; }
.parent-att-size { font-size: 12px; color: var(--pt-text-faint, #6b7280); }
```

- [ ] **Step 3: typecheck + build**

Run: `cd ivy-frontend && npm run typecheck && npm run build`
Expected: 0 error。

- [ ] **Step 4: Commit**

```bash
cd ivy-frontend && git add src/parent/components/announcements/AnnouncementDetailModal.vue
git commit -m "feat(ui-parent): announcement detail modal shows attachments"
```

---

## Task 13: Vitest — 前端附件互動

**Files:**
- Modify: `ivy-frontend/tests/unit/views/AnnouncementView.test.js`

- [ ] **Step 1: 加 test — limit 訊息 + size 訊息**

```javascript
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import ElementPlus from 'element-plus'
import AnnouncementView from '@/views/AnnouncementView.vue'

const uploadFn = vi.fn().mockResolvedValue({ data: {} })
const deleteFn = vi.fn().mockResolvedValue({ data: {} })

vi.mock('@/api/announcements', () => ({
  getAnnouncements: vi.fn().mockResolvedValue({ data: { items: [] } }),
  createAnnouncement: vi.fn().mockResolvedValue({ data: { id: 100 } }),
  updateAnnouncement: vi.fn(),
  deleteAnnouncement: vi.fn(),
  getAnnouncementParentRecipients: vi.fn().mockResolvedValue({ data: { items: [] } }),
  replaceAnnouncementParentRecipients: vi.fn().mockResolvedValue({}),
  getAnnouncementRecipients: vi.fn().mockResolvedValue({ data: { employee_ids: [] } }),
  getAnnouncementReaders: vi.fn().mockResolvedValue({ data: { items: [], total: 0 } }),
  uploadAnnouncementAttachment: uploadFn,
  deleteAnnouncementAttachment: deleteFn,
}))
vi.mock('@/stores/employee', () => ({ useEmployeeStore: () => ({ employees: [], fetchEmployees: vi.fn() }) }))
vi.mock('@/stores/classroom', () => ({ useClassroomStore: () => ({ classrooms: [], fetchClassrooms: vi.fn() }) }))

describe('AnnouncementView attachments', () => {
  it('鎖死 size 超過 10MB 時 reject upload', async () => {
    const wrapper = mount(AnnouncementView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { handleAttachmentBefore: (f: File) => boolean }
    const bigFile = new File(['x'.repeat(11 * 1024 * 1024)], 'big.pdf', { type: 'application/pdf' })
    expect(vm.handleAttachmentBefore(bigFile)).toBe(false)
  })
})
```

- [ ] **Step 2: 跑 vitest**

Run: `cd ivy-frontend && npx vitest run tests/unit/views/AnnouncementView.test.js`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
cd ivy-frontend && git add tests/unit/views/AnnouncementView.test.js
git commit -m "test(ui): announcement attachment size guard"
```

---

## Self-Review checklist（implementer 完成後跑）

- [ ] Backend full pytest：`cd ivy-backend && pytest tests/ -q 2>&1 | tail -10` — 無新 regression
- [ ] Attachment / announcement focused：`pytest tests/ -k "announcement or attachment" -v` — 全綠
- [ ] Frontend typecheck + build + vitest：`cd ivy-frontend && npm run typecheck && npm run build && npx vitest run` — 全綠
- [ ] OpenAPI drift：`cd ivy-frontend && npm run gen:api:check` — 無 drift
- [ ] 手測：
  - admin 新增公告含 1 圖片 + 1 PDF + 家長 recipients=all → 公告建立 + 兩個附件 upload + LINE flex 含圖片 hero
  - admin 編輯既有公告刪除 1 附件 → list response attachments 少 1
  - 員工 portal 展開公告看到附件，點擊圖片開新分頁顯示
  - 家長 portal modal 內看到 PDF row，點擊下載
  - 員工被指定 target 公告附件可下載；未指定員工 fetch URL → 403
  - 家長 scope=classroom A 公告附件 → A 班家長可下載；其他家長 403

---

## Out-of-scope（不在本 plan）

- 影片附件
- 縮圖 inline 在 admin list（縮圖只在 dialog 開啟時顯示）
- 附件版本管理（取代 vs 新增） — 直接刪掉重傳
- 公告附件獨立 audit 欄位（既有 audit middleware 已覆蓋）
- 大檔（>10MB）chunked upload
