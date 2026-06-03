"""廠商付款簽收 API 回歸測試。"""

import base64
import io
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts, router as auth_router
from api.vendor_payments import router as vendor_payments_router
from models.database import Base, Employee, User
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


@pytest.fixture
def client_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "vendor-payments.sqlite"
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

    # 將 portfolio_storage root 指到 tmp_path 隔離
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    import utils.portfolio_storage as ps

    ps.reset_portfolio_storage()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(vendor_payments_router)

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
    # permission_names: list[str] | Permission | str；單一 Permission 自動 wrap
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


def _payment_payload(**overrides) -> dict:
    base = {
        "payment_date": "2026-05-15",
        "vendor_name": "好棒棒清潔用品行",
        "amount": "1200.00",
        "payment_method": "cash",
        "description": "5 月清潔用品",
        "invoice_number": "AB-12345678",
        "notes": "送貨單已對齊",
    }
    base.update(overrides)
    return base


class TestVendorPaymentCRUD:
    def test_happy_path_create_list_update_sign(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_admin",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )

        _login(client, "vp_admin")

        # 建立
        res = client.post("/api/vendor-payments", json=_payment_payload())
        assert res.status_code == 201, res.text
        pid = res.json()["id"]

        # 列表
        res = client.get("/api/vendor-payments")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["status"] == "pending"
        assert item["vendor_name"] == "好棒棒清潔用品行"
        assert item["amount"] == 1200.0

        # 更新（改備註）
        res = client.put(
            f"/api/vendor-payments/{pid}",
            json={"notes": "已對帳 OK", "description": "5 月清潔用品（更新）"},
        )
        assert res.status_code == 200

        res = client.get(f"/api/vendor-payments/{pid}")
        assert res.json()["notes"] == "已對帳 OK"
        assert res.json()["description"] == "5 月清潔用品（更新）"

        # 簽收
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text

        res = client.get(f"/api/vendor-payments/{pid}")
        body = res.json()
        assert body["status"] == "signed"
        assert body["signature_kind"] == "drawn"
        assert body["has_signature"] is True
        assert body["signed_at"] is not None

        # 不能再簽收
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 409

        # 取得簽名圖
        res = client.get(f"/api/vendor-payments/{pid}/signature")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/")

        # 刪除
        res = client.delete(f"/api/vendor-payments/{pid}")
        assert res.status_code == 200
        assert client.get(f"/api/vendor-payments/{pid}").status_code == 404

    def test_read_only_user_cannot_write(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "vp_reader", Permission.VENDOR_PAYMENT_READ)

        _login(client, "vp_reader")
        # 讀可
        res = client.get("/api/vendor-payments")
        assert res.status_code == 200
        # 寫不可
        res = client.post("/api/vendor-payments", json=_payment_payload())
        assert res.status_code == 403

    def test_user_without_permission_cannot_read(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            # 無 VENDOR_PAYMENT_*
            _make_user(session, "no_perm", Permission.DASHBOARD)

        _login(client, "no_perm")
        res = client.get("/api/vendor-payments")
        assert res.status_code == 403


class TestVendorPaymentValidation:
    def test_amount_must_be_non_negative(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_val",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_val")
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(amount="-1"),
        )
        assert res.status_code == 422

    def test_payment_method_must_be_valid(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_val2",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_val2")
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(payment_method="bitcoin"),
        )
        assert res.status_code == 422


class TestVendorPaymentFilters:
    def test_filter_by_status_and_vendor(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_filter",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_filter")
        ids = []
        for name in ["A 公司", "B 行號", "A 公司"]:
            res = client.post(
                "/api/vendor-payments",
                json=_payment_payload(vendor_name=name),
            )
            ids.append(res.json()["id"])
        # 簽掉第一筆
        client.post(
            f"/api/vendor-payments/{ids[0]}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        # 用 vendor_name 篩
        res = client.get("/api/vendor-payments", params={"vendor_name": "A 公司"})
        assert res.json()["total"] == 2
        # 用 status 篩
        res = client.get("/api/vendor-payments", params={"status": "signed"})
        assert res.json()["total"] == 1
        res = client.get("/api/vendor-payments", params={"status": "pending"})
        assert res.json()["total"] == 2


class TestVendorPaymentSummary:
    def test_summary_breaks_down_by_status_over_range(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_sum",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_sum")

        # 三筆 5 月、一筆 4 月（用來驗 range 篩選會排除）
        ids = []
        for amt, day in [
            ("1000", "2026-05-01"),
            ("2500", "2026-05-10"),
            ("4000", "2026-05-20"),
        ]:
            res = client.post(
                "/api/vendor-payments",
                json=_payment_payload(amount=amt, payment_date=day),
            )
            ids.append(res.json()["id"])
        client.post(
            "/api/vendor-payments",
            json=_payment_payload(amount="9999", payment_date="2026-04-15"),
        )
        # 簽掉其中一筆 5 月（2500）
        client.post(
            f"/api/vendor-payments/{ids[1]}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )

        # 5 月區間彙總：總 3 筆 7500，待簽 2 筆 5000，已簽 1 筆 2500
        res = client.get(
            "/api/vendor-payments/summary",
            params={"start_date": "2026-05-01", "end_date": "2026-05-31"},
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
        # summary 不吃 status：即使帶 status=signed 也要回全狀態拆分
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_sum2",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_sum2")
        client.post("/api/vendor-payments", json=_payment_payload(amount="100"))
        res = client.get("/api/vendor-payments/summary", params={"status": "signed"})
        assert res.status_code == 200, res.text
        # status 是未知 query param，被忽略；pending 那筆仍計入
        assert res.json()["pending_count"] == 1

    def test_summary_empty_range_returns_zeros(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "vp_sum3", ["VENDOR_PAYMENT_READ"])
        _login(client, "vp_sum3")
        res = client.get(
            "/api/vendor-payments/summary",
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
            _make_user(session, "vp_sum_noperm", Permission.DASHBOARD)
        _login(client, "vp_sum_noperm")
        res = client.get("/api/vendor-payments/summary")
        assert res.status_code == 403

    def test_summary_route_not_shadowed_by_id_route(self, client_with_db):
        # /summary 不可被 /{payment_id} 吃掉（否則會 422 / 404）
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(session, "vp_sum4", ["VENDOR_PAYMENT_READ"])
        _login(client, "vp_sum4")
        res = client.get("/api/vendor-payments/summary")
        assert res.status_code == 200, res.text


class TestVendorPaymentSignature:
    def test_sign_rejects_too_small_payload(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_sig",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_sig")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]
        # tiny base64
        tiny = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": tiny},
        )
        assert res.status_code == 400

    def test_sign_rejects_corrupt_signature(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_sig2",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_sig2")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]
        # 隨機 bytes（非 PNG magic）
        fake = "data:image/png;base64," + base64.b64encode(b"X" * 500).decode()
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": fake},
        )
        assert res.status_code == 400


class TestVendorPaymentAttachments:
    def test_upload_and_delete_attachment(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_att",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_att")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]

        # 上傳 PNG
        png = _make_png(100)
        res = client.post(
            f"/api/vendor-payments/{pid}/attachments",
            files={"file": ("receipt.png", png, "image/png")},
        )
        assert res.status_code == 201, res.text
        meta = res.json()
        assert meta["filename"].endswith(".png")
        key = meta["key"]

        # 列表帶出 attachment
        res = client.get(f"/api/vendor-payments/{pid}")
        assert len(res.json()["attachments"]) == 1

        # 下載
        res = client.get(
            f"/api/vendor-payments/{pid}/attachments/download",
            params={"key": key},
        )
        assert res.status_code == 200

        # 刪除
        res = client.delete(
            f"/api/vendor-payments/{pid}/attachments",
            params={"key": key},
        )
        assert res.status_code == 200
        res = client.get(f"/api/vendor-payments/{pid}")
        assert res.json()["attachments"] == []

    def test_reject_unsupported_extension(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_att2",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_att2")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]
        res = client.post(
            f"/api/vendor-payments/{pid}/attachments",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert res.status_code == 400
