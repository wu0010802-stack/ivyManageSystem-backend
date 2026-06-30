import base64
import io
import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts, router as auth_router
from api.misc_receipts import router as misc_receipts_router
from models.database import Base, Employee, User
from models.misc_receipt import MiscReceipt, RECEIPT_CATEGORIES
from utils.auth import hash_password
from utils.permissions import Permission

# 1x1 PNG（合法 magic bytes）— base64 編碼
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_png(size: int = 200) -> bytes:
    """製造一張合法 PNG，回 bytes。"""
    img = Image.new("RGB", (size, size), color=(180, 200, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_data_url(content: bytes | None = None) -> str:
    raw = content or _make_png()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_misc_receipt_amount_must_be_positive(test_db_session):
    row = MiscReceipt(
        receipt_date=date(2026, 6, 1),
        payer_name="某基金會",
        category="donation",
        amount=0,
        payment_method="cash",
        status="pending",
        attachments=[],
    )
    test_db_session.add(row)
    with pytest.raises(IntegrityError):
        test_db_session.flush()


def test_misc_receipt_categories_constant():
    assert set(RECEIPT_CATEGORIES) == {
        "rent",
        "donation",
        "subsidy",
        "secondhand_sale",
        "refund_recovery",
        "other",
    }


def test_misc_receipt_permissions_exist():
    from utils.permissions import Permission, PERMISSION_LABELS

    assert Permission.MISC_RECEIPT_READ.value == "MISC_RECEIPT_READ"
    assert Permission.MISC_RECEIPT_WRITE.value == "MISC_RECEIPT_WRITE"
    assert PERMISSION_LABELS["MISC_RECEIPT_READ"] == "雜項收款 (檢視)"
    assert PERMISSION_LABELS["MISC_RECEIPT_WRITE"] == "雜項收款 (編輯/簽收)"


def test_misc_receipt_in_finance_roles():
    from utils.permissions import ROLE_TEMPLATES

    for role in ("hr", "supervisor", "accountant"):
        assert "MISC_RECEIPT_READ" in ROLE_TEMPLATES[role]
        assert "MISC_RECEIPT_WRITE" in ROLE_TEMPLATES[role]


# ─────────────────────────────────────────────────────────────────────────────
# 端點測試 fixtures 與 helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "misc-receipts.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    # portfolio_storage root 指到 tmp_path 隔離
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    import utils.portfolio_storage as ps

    ps.reset_portfolio_storage()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(misc_receipts_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()
    ps.reset_portfolio_storage()


def _make_user(
    session,
    username: str,
    permission_names,
    password: str = "TempPass123",
) -> int:
    """建立測試用 Employee + User，回傳 employee.id。"""
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    elif permission_names is None:
        permission_names = []
    else:
        permission_names = list(permission_names)
    emp = Employee(
        employee_id=f"E-{username}",
        name=f"員工{username}",
        base_salary=32000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=permission_names,
        is_active=True,
        employee_id=emp.id,
    )
    session.add(user)
    session.commit()
    return emp.id


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _receipt_payload(**overrides) -> dict:
    base = {
        # 相對 today 避免 receipt_date 守衛（禁未來日、回補 90 天）時間炸彈
        "receipt_date": date.today().isoformat(),
        "payer_name": "某慈善基金會",
        "category": "donation",
        "amount": "5000.00",
        "payment_method": "bank_transfer",
        "description": "六月捐款",
        "receipt_number": "RCP-20260601",
        "notes": "已確認入帳",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# CRUD / 生命週期
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptCRUD:
    def test_happy_path_create_list_update_sign(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_admin",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )

        _login(client, "mr_admin")

        # 建立
        res = client.post("/api/misc-receipts", json=_receipt_payload())
        assert res.status_code == 201, res.text
        rid = res.json()["id"]

        # 列表
        res = client.get("/api/misc-receipts")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["status"] == "pending"
        assert item["payer_name"] == "某慈善基金會"
        assert item["amount"] == 5000.0
        assert item["category"] == "donation"

        # 更新（改備註）
        res = client.put(
            f"/api/misc-receipts/{rid}",
            json={"notes": "已對帳 OK", "description": "六月捐款（更新）"},
        )
        assert res.status_code == 200

        res = client.get(f"/api/misc-receipts/{rid}")
        assert res.json()["notes"] == "已對帳 OK"
        assert res.json()["description"] == "六月捐款（更新）"

        # 簽收
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text

        res = client.get(f"/api/misc-receipts/{rid}")
        body = res.json()
        assert body["status"] == "signed"
        assert body["signature_kind"] == "drawn"
        assert body["has_signature"] is True
        assert body["signed_at"] is not None

        # 不能再簽收
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 409

        # 取得簽名圖
        res = client.get(f"/api/misc-receipts/{rid}/signature")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/")

        # 已簽收不可刪除
        res = client.delete(f"/api/misc-receipts/{rid}")
        assert res.status_code == 409, res.text
        assert client.get(f"/api/misc-receipts/{rid}").status_code == 200

    def test_cannot_update_after_signed(self, client_with_db):
        """P2-E：已簽收的雜項收款不可再被編輯。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_admin2",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_admin2")

        res = client.post("/api/misc-receipts", json=_receipt_payload(amount="1200.00"))
        assert res.status_code == 201, res.text
        rid = res.json()["id"]

        # 簽收
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text

        # 簽收後嘗試改金額 → 須拒絕（409）
        res = client.put(f"/api/misc-receipts/{rid}", json={"amount": "999999.00"})
        assert res.status_code == 409, res.text

        res = client.get(f"/api/misc-receipts/{rid}")
        assert res.json()["amount"] == 1200.0
        assert res.json()["status"] == "signed"

    def test_read_only_user_cannot_write(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_reader", Permission.MISC_RECEIPT_READ)

        _login(client, "mr_reader")
        # 讀可
        res = client.get("/api/misc-receipts")
        assert res.status_code == 200
        # 寫不可
        res = client.post("/api/misc-receipts", json=_receipt_payload())
        assert res.status_code == 403

    def test_user_without_permission_cannot_read(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_noperm", Permission.DASHBOARD)

        _login(client, "mr_noperm")
        res = client.get("/api/misc-receipts")
        assert res.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 刪除守衛
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptDeleteGuard:
    """P1：已簽收收款不可硬刪，否則已發生收入會從財報消失、簽名佐證遺失。"""

    def _admin_client(self, client_with_db, username):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                username,
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, username)
        return client

    def test_can_delete_pending(self, client_with_db):
        client = self._admin_client(client_with_db, "mr_del_pending")
        res = client.post("/api/misc-receipts", json=_receipt_payload())
        rid = res.json()["id"]
        # pending 可刪
        res = client.delete(f"/api/misc-receipts/{rid}")
        assert res.status_code == 200, res.text
        assert client.get(f"/api/misc-receipts/{rid}").status_code == 404

    def test_cannot_delete_after_signed(self, client_with_db):
        client = self._admin_client(client_with_db, "mr_del_signed")
        res = client.post("/api/misc-receipts", json=_receipt_payload())
        rid = res.json()["id"]
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text
        # 簽收後刪除須被拒（409）
        res = client.delete(f"/api/misc-receipts/{rid}")
        assert res.status_code == 409, res.text
        body = client.get(f"/api/misc-receipts/{rid}")
        assert body.status_code == 200
        assert body.json()["status"] == "signed"


# ─────────────────────────────────────────────────────────────────────────────
# 附件簽收守衛
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptAttachmentSignedGuard:
    """P3-3(b)：已簽收收款的附件不可增刪（對齊 update/delete 簽收守衛）。"""

    def _create_pending(self, client, session_factory, username):
        with session_factory() as session:
            _make_user(session, username, ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"])
        _login(client, username)
        res = client.post("/api/misc-receipts", json=_receipt_payload())
        assert res.status_code == 201, res.text
        return res.json()["id"]

    def _png(self):
        return base64.b64decode(_png_data_url().split(",", 1)[1])

    def test_cannot_upload_attachment_after_signed(self, client_with_db):
        client, sf = client_with_db
        rid = self._create_pending(client, sf, "mr_att_up")
        # 簽收
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text
        # 簽收後上傳附件 → 409
        res = client.post(
            f"/api/misc-receipts/{rid}/attachments",
            files={"file": ("inv.png", self._png(), "image/png")},
        )
        assert res.status_code == 409, res.text

    def test_cannot_delete_attachment_after_signed(self, client_with_db):
        client, sf = client_with_db
        rid = self._create_pending(client, sf, "mr_att_del")
        # pending 時先上傳成功
        res = client.post(
            f"/api/misc-receipts/{rid}/attachments",
            files={"file": ("inv.png", self._png(), "image/png")},
        )
        assert res.status_code == 201, res.text
        key = res.json()["key"]
        # 簽收
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text
        # 簽收後刪附件 → 409
        res = client.delete(
            f"/api/misc-receipts/{rid}/attachments", params={"key": key}
        )
        assert res.status_code == 409, res.text

    def test_pending_attachment_still_mutable(self, client_with_db):
        """sanity：pending 狀態附件仍可增刪。"""
        client, sf = client_with_db
        rid = self._create_pending(client, sf, "mr_att_pending")
        res = client.post(
            f"/api/misc-receipts/{rid}/attachments",
            files={"file": ("inv.png", self._png(), "image/png")},
        )
        assert res.status_code == 201, res.text
        key = res.json()["key"]
        res = client.delete(
            f"/api/misc-receipts/{rid}/attachments", params={"key": key}
        )
        assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────────
# 欄位驗證
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptValidation:
    def test_amount_must_be_positive(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_val",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_val")
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(amount="-1"),
        )
        assert res.status_code == 422

    def test_payment_method_must_be_valid(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_val2",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_val2")
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(payment_method="bitcoin"),
        )
        assert res.status_code == 422

    def test_bad_category_rejected(self, client_with_db):
        """無效的 category 值必須被 Pydantic validator 拒絕（422）。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_cat_bad",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_cat_bad")
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(category="bogus"),
        )
        assert res.status_code == 422

    def test_valid_category_persists(self, client_with_db):
        """合法的 category 值應成功建立並可查詢。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_cat_ok",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_cat_ok")
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(category="subsidy"),
        )
        assert res.status_code == 201, res.text
        rid = res.json()["id"]
        res = client.get(f"/api/misc-receipts/{rid}")
        assert res.json()["category"] == "subsidy"

    def test_rejects_future_receipt_date(self, client_with_db):
        """P1/P2：收款日不可填未來日。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "mr_future", ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"]
            )
        _login(client, "mr_future")
        future = (date.today() + timedelta(days=1)).isoformat()
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(receipt_date=future),
        )
        assert res.status_code == 422

    def test_rejects_receipt_date_older_than_90_days(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_old", ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"])
        _login(client, "mr_old")
        too_old = (date.today() - timedelta(days=91)).isoformat()
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(receipt_date=too_old),
        )
        assert res.status_code == 422

    def test_accepts_receipt_date_within_window(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_edge", ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"])
        _login(client, "mr_edge")
        within = (date.today() - timedelta(days=89)).isoformat()
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(receipt_date=within),
        )
        assert res.status_code == 201, res.text

    def test_amount_zero_rejected(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_zero", ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"])
        _login(client, "mr_zero")
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(amount="0"),
        )
        assert res.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# 篩選（category / status）
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptFilters:
    def test_filter_by_status_and_payer(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_filter",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_filter")
        ids = []
        for name in ["基金會 A", "機構 B", "基金會 A"]:
            res = client.post(
                "/api/misc-receipts",
                json=_receipt_payload(payer_name=name),
            )
            ids.append(res.json()["id"])
        # 簽掉第一筆
        client.post(
            f"/api/misc-receipts/{ids[0]}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        # 用 payer_name 篩
        res = client.get("/api/misc-receipts", params={"payer_name": "基金會 A"})
        assert res.json()["total"] == 2
        # 用 status 篩
        res = client.get("/api/misc-receipts", params={"status": "signed"})
        assert res.json()["total"] == 1
        res = client.get("/api/misc-receipts", params={"status": "pending"})
        assert res.json()["total"] == 2

    def test_filter_by_category(self, client_with_db):
        """category 篩選只回傳對應類別的紀錄。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_cat_filter",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_cat_filter")
        client.post("/api/misc-receipts", json=_receipt_payload(category="donation"))
        client.post("/api/misc-receipts", json=_receipt_payload(category="rent"))
        client.post("/api/misc-receipts", json=_receipt_payload(category="rent"))

        res = client.get("/api/misc-receipts", params={"category": "rent"})
        assert res.json()["total"] == 2
        res = client.get("/api/misc-receipts", params={"category": "donation"})
        assert res.json()["total"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Summary 彙總
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptSummary:
    def test_summary_breaks_down_by_status_over_range(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_sum",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_sum")

        # 相對日期：三筆「上月」、一筆「上上月」
        today = date.today()
        last_month_end = today.replace(day=1) - timedelta(days=1)
        lm = last_month_end.replace(day=1)
        prev_month_end = lm - timedelta(days=1)
        ids = []
        for amt, d in [
            ("1000", lm.replace(day=1)),
            ("2500", lm.replace(day=10)),
            ("4000", lm.replace(day=20)),
        ]:
            res = client.post(
                "/api/misc-receipts",
                json=_receipt_payload(amount=amt, receipt_date=d.isoformat()),
            )
            ids.append(res.json()["id"])
        client.post(
            "/api/misc-receipts",
            json=_receipt_payload(
                amount="9999", receipt_date=prev_month_end.replace(day=15).isoformat()
            ),
        )
        # 簽掉其中一筆上月（2500）
        client.post(
            f"/api/misc-receipts/{ids[1]}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )

        # 上月區間彙總：總 3 筆 7500，待簽 2 筆 5000，已簽 1 筆 2500
        res = client.get(
            "/api/misc-receipts/summary",
            params={
                "start_date": lm.isoformat(),
                "end_date": last_month_end.isoformat(),
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total_count"] == 3
        assert body["total_amount"] == 7500.0
        assert body["pending_count"] == 2
        assert body["pending_amount"] == 5000.0
        assert body["signed_count"] == 1
        assert body["signed_amount"] == 2500.0

    def test_summary_ignores_status_filter_param(self, client_with_db):
        """summary 不吃 status：帶 status=signed 仍回全狀態拆分。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_sum2",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_sum2")
        client.post("/api/misc-receipts", json=_receipt_payload(amount="100"))
        res = client.get("/api/misc-receipts/summary", params={"status": "signed"})
        assert res.status_code == 200, res.text
        # status 是未知 query param，被忽略；pending 那筆仍計入
        assert res.json()["pending_count"] == 1

    def test_summary_empty_range_returns_zeros(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_sum3", ["MISC_RECEIPT_READ"])
        _login(client, "mr_sum3")
        res = client.get(
            "/api/misc-receipts/summary",
            params={"start_date": "2099-01-01", "end_date": "2099-12-31"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body == {
            "total_count": 0,
            "total_amount": 0.0,
            "pending_count": 0,
            "pending_amount": 0.0,
            "signed_count": 0,
            "signed_amount": 0.0,
        }

    def test_summary_requires_read_permission(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_sum_noperm", Permission.DASHBOARD)
        _login(client, "mr_sum_noperm")
        res = client.get("/api/misc-receipts/summary")
        assert res.status_code == 403

    def test_summary_route_not_shadowed_by_id_route(self, client_with_db):
        """/summary 不可被 /{receipt_id} 吃掉（否則會 422 / 404）。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "mr_sum4", ["MISC_RECEIPT_READ"])
        _login(client, "mr_sum4")
        res = client.get("/api/misc-receipts/summary")
        assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────────
# 簽名驗證
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptSignature:
    def test_sign_rejects_too_small_payload(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_sig",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_sig")
        rid = client.post("/api/misc-receipts", json=_receipt_payload()).json()["id"]
        # tiny base64
        tiny = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": tiny},
        )
        assert res.status_code == 400

    def test_sign_rejects_corrupt_signature(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_sig2",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_sig2")
        rid = client.post("/api/misc-receipts", json=_receipt_payload()).json()["id"]
        # 隨機 bytes（非 PNG magic）
        fake = "data:image/png;base64," + base64.b64encode(b"X" * 500).decode()
        res = client.post(
            f"/api/misc-receipts/{rid}/sign",
            json={"signature_kind": "drawn", "signature_data": fake},
        )
        assert res.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# 附件
# ─────────────────────────────────────────────────────────────────────────────


class TestMiscReceiptAttachments:
    def test_upload_and_delete_attachment(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_att",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_att")
        rid = client.post("/api/misc-receipts", json=_receipt_payload()).json()["id"]

        # 上傳 PNG
        png = _make_png(100)
        res = client.post(
            f"/api/misc-receipts/{rid}/attachments",
            files={"file": ("receipt.png", png, "image/png")},
        )
        assert res.status_code == 201, res.text
        meta = res.json()
        assert meta["filename"].endswith(".png")
        key = meta["key"]

        # 列表帶出 attachment
        res = client.get(f"/api/misc-receipts/{rid}")
        assert len(res.json()["attachments"]) == 1

        # 下載
        res = client.get(
            f"/api/misc-receipts/{rid}/attachments/download",
            params={"key": key},
        )
        assert res.status_code == 200

        # 刪除
        res = client.delete(
            f"/api/misc-receipts/{rid}/attachments",
            params={"key": key},
        )
        assert res.status_code == 200
        res = client.get(f"/api/misc-receipts/{rid}")
        assert res.json()["attachments"] == []

    def test_reject_unsupported_extension(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "mr_att2",
                ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
            )
        _login(client, "mr_att2")
        rid = client.post("/api/misc-receipts", json=_receipt_payload()).json()["id"]
        res = client.post(
            f"/api/misc-receipts/{rid}/attachments",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert res.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# payer_name LIKE 跳脫
# ─────────────────────────────────────────────────────────────────────────────


class TestPayerNameSearchEscape:
    """payer_name 篩選須跳脫 LIKE 萬用字元，避免 '_' / '%' over-match。"""

    def _seed(self, session):
        _make_user(
            session,
            "mr_search",
            ["MISC_RECEIPT_READ", "MISC_RECEIPT_WRITE"],
        )

    def _create(self, client, payer_name):
        res = client.post(
            "/api/misc-receipts",
            json=_receipt_payload(payer_name=payer_name),
        )
        assert res.status_code == 201, res.text

    def test_underscore_not_treated_as_wildcard(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            self._seed(session)
        _login(client, "mr_search")

        self._create(client, "A_B 基金會")
        self._create(client, "AXB 基金會")

        res = client.get("/api/misc-receipts", params={"payer_name": "A_B"})
        assert res.status_code == 200, res.text
        body = res.json()
        names = sorted(item["payer_name"] for item in body["items"])
        assert names == ["A_B 基金會"], names
        assert body["total"] == 1

    def test_percent_not_treated_as_wildcard(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            self._seed(session)
        _login(client, "mr_search")

        self._create(client, "C%D 機構")
        self._create(client, "CZZD 機構")

        res = client.get("/api/misc-receipts", params={"payer_name": "C%D"})
        assert res.status_code == 200, res.text
        body = res.json()
        names = sorted(item["payer_name"] for item in body["items"])
        assert names == ["C%D 機構"], names
        assert body["total"] == 1
