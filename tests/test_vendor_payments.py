"""廠商付款簽收 API 回歸測試。"""

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
        # 相對 today（台灣時區）以避免 payment_date 守衛（禁未來日、回補 90 天）
        # 隨時間流逝把寫死日期推出視窗造成時間炸彈。
        "payment_date": date.today().isoformat(),
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

        # 已簽收不可刪除（與 update 守衛對稱，保留簽名佐證與財報支出）
        res = client.delete(f"/api/vendor-payments/{pid}")
        assert res.status_code == 409, res.text
        assert client.get(f"/api/vendor-payments/{pid}").status_code == 200

    def test_cannot_update_after_signed(self, client_with_db):
        """P2-E：已簽收的廠商付款不可再被編輯（金額等簽名佐證須不可竄改）。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                "vp_admin2",
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, "vp_admin2")

        res = client.post(
            "/api/vendor-payments", json=_payment_payload(amount="1200.00")
        )
        assert res.status_code == 201, res.text
        pid = res.json()["id"]

        # 簽收
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text

        # 簽收後嘗試改金額 → 須拒絕（409），金額不可被竄改
        res = client.put(f"/api/vendor-payments/{pid}", json={"amount": "999999.00"})
        assert res.status_code == 409, res.text

        res = client.get(f"/api/vendor-payments/{pid}")
        assert res.json()["amount"] == 1200.0
        assert res.json()["status"] == "signed"

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


class TestVendorPaymentDeleteGuard:
    """P1：已簽收付款不可硬刪，否則已發生支出會從財報消失、簽名佐證遺失。"""

    def _admin_client(self, client_with_db, username):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session,
                username,
                ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
            )
        _login(client, username)
        return client

    def test_can_delete_pending(self, client_with_db):
        client = self._admin_client(client_with_db, "vp_del_pending")
        res = client.post("/api/vendor-payments", json=_payment_payload())
        pid = res.json()["id"]
        # pending 可刪
        res = client.delete(f"/api/vendor-payments/{pid}")
        assert res.status_code == 200, res.text
        assert client.get(f"/api/vendor-payments/{pid}").status_code == 404

    def test_cannot_delete_after_signed(self, client_with_db):
        client = self._admin_client(client_with_db, "vp_del_signed")
        res = client.post("/api/vendor-payments", json=_payment_payload())
        pid = res.json()["id"]
        res = client.post(
            f"/api/vendor-payments/{pid}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )
        assert res.status_code == 200, res.text
        # 簽收後刪除須被拒（409），原始 row 與支出保留
        res = client.delete(f"/api/vendor-payments/{pid}")
        assert res.status_code == 409, res.text
        body = client.get(f"/api/vendor-payments/{pid}")
        assert body.status_code == 200
        assert body.json()["status"] == "signed"


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

    def test_amount_zero_rejected(self, client_with_db):
        """P3：金額須大於 0，0 元會產生稽核雜訊。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_zero", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_zero")
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(amount="0"),
        )
        assert res.status_code == 422

    def test_amount_zero_rejected_on_update(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_zero_u", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_zero_u")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]
        res = client.put(f"/api/vendor-payments/{pid}", json={"amount": "0"})
        assert res.status_code == 422

    def test_rejects_future_payment_date(self, client_with_db):
        """P1/P2：付款日不可填未來日，否則可把支出搬到尚未到來的月份。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_future", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_future")
        future = (date.today() + timedelta(days=1)).isoformat()
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(payment_date=future),
        )
        assert res.status_code == 422

    def test_rejects_payment_date_older_than_90_days(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_old", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_old")
        too_old = (date.today() - timedelta(days=91)).isoformat()
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(payment_date=too_old),
        )
        assert res.status_code == 422

    def test_accepts_payment_date_within_window(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_edge", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_edge")
        within = (date.today() - timedelta(days=89)).isoformat()
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(payment_date=within),
        )
        assert res.status_code == 201, res.text

    def test_update_rejects_future_payment_date(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _make_user(
                session, "vp_upd_fut", ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"]
            )
        _login(client, "vp_upd_fut")
        pid = client.post("/api/vendor-payments", json=_payment_payload()).json()["id"]
        future = (date.today() + timedelta(days=5)).isoformat()
        res = client.put(f"/api/vendor-payments/{pid}", json={"payment_date": future})
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

        # 相對日期：三筆「上月」、一筆「上上月」（驗 range 篩選會排除上上月那筆）。
        # 全部落在 payment_date 守衛 90 天回補視窗內，避免寫死日期造成時間炸彈。
        today = date.today()
        last_month_end = today.replace(day=1) - timedelta(days=1)  # 上月最後一天
        lm = last_month_end.replace(day=1)  # 上月一日
        prev_month_end = lm - timedelta(days=1)  # 上上月最後一天
        ids = []
        for amt, d in [
            ("1000", lm.replace(day=1)),
            ("2500", lm.replace(day=10)),
            ("4000", lm.replace(day=20)),
        ]:
            res = client.post(
                "/api/vendor-payments",
                json=_payment_payload(amount=amt, payment_date=d.isoformat()),
            )
            ids.append(res.json()["id"])
        client.post(
            "/api/vendor-payments",
            json=_payment_payload(
                amount="9999", payment_date=prev_month_end.replace(day=15).isoformat()
            ),
        )
        # 簽掉其中一筆上月（2500）
        client.post(
            f"/api/vendor-payments/{ids[1]}/sign",
            json={"signature_kind": "drawn", "signature_data": _png_data_url()},
        )

        # 上月區間彙總：總 3 筆 7500，待簽 2 筆 5000，已簽 1 筆 2500
        res = client.get(
            "/api/vendor-payments/summary",
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


class TestVendorNameSearchEscape:
    """[C4] vendor_name 篩選須跳脫 LIKE 萬用字元，避免 '_' / '%' over-match。"""

    def _seed(self, session):
        _make_user(
            session,
            "vp_search",
            ["VENDOR_PAYMENT_READ", "VENDOR_PAYMENT_WRITE"],
        )

    def _create(self, client, vendor_name):
        res = client.post(
            "/api/vendor-payments",
            json=_payment_payload(vendor_name=vendor_name),
        )
        assert res.status_code == 201, res.text

    def test_underscore_not_treated_as_wildcard(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            self._seed(session)
        _login(client, "vp_search")

        # 'A_B' 含字面底線；'AXB' 只有在 '_' 被當萬用字元時才會被誤匹配。
        self._create(client, "A_B 清潔行")
        self._create(client, "AXB 清潔行")

        res = client.get("/api/vendor-payments", params={"vendor_name": "A_B"})
        assert res.status_code == 200, res.text
        body = res.json()
        names = sorted(item["vendor_name"] for item in body["items"])
        # 只應命中字面 'A_B'，不應因 '_' 萬用字元帶出 'AXB'
        assert names == ["A_B 清潔行"], names
        assert body["total"] == 1

    def test_percent_not_treated_as_wildcard(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            self._seed(session)
        _login(client, "vp_search")

        # 'C%D' 含字面百分比；'CZZD' 只有在 '%' 被當萬用字元時才會被誤匹配。
        self._create(client, "C%D 文具")
        self._create(client, "CZZD 文具")

        res = client.get("/api/vendor-payments", params={"vendor_name": "C%D"})
        assert res.status_code == 200, res.text
        body = res.json()
        names = sorted(item["vendor_name"] for item in body["items"])
        assert names == ["C%D 文具"], names
        assert body["total"] == 1
