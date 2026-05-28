"""家長端 BusinessError subclass 與 envelope handler 契約測試。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.business_errors.parent import (
    BindCodeAlreadyUsed,
    BindCodeExpired,
    BindCodeInvalid,
    ConsentRequired,
    ContactBookNotPublished,
    DsrRequestInvalid,
    LineBindingExpired,
    LineBindingNotFound,
    LineProfileFetchFailed,
    ParentNotAuthorized,
    PortalDataUnavailable,
    StudentNotFound,
    StudentNotLinkedToParent,
)
from utils.exception_handlers import register_exception_handlers


def test_bind_code_invalid_default():
    err = BindCodeInvalid()
    assert err.code == "BIND_CODE_INVALID"
    assert err.http_status == 400
    assert err.message == "綁定碼無效或已過期"


def test_bind_code_invalid_custom_message():
    err = BindCodeInvalid("自訂訊息")
    assert err.code == "BIND_CODE_INVALID"
    assert err.message == "自訂訊息"


def test_bind_code_already_used_409():
    err = BindCodeAlreadyUsed()
    assert err.http_status == 409


def test_line_binding_expired_401():
    err = LineBindingExpired()
    assert err.http_status == 401
    assert err.code == "LINE_BINDING_EXPIRED"


def test_student_not_found_404():
    err = StudentNotFound()
    assert err.http_status == 404


def test_student_not_linked_403():
    err = StudentNotLinkedToParent()
    assert err.http_status == 403


def test_line_profile_fetch_failed_502():
    err = LineProfileFetchFailed()
    assert err.http_status == 502


def test_contact_book_not_published_404():
    err = ContactBookNotPublished()
    assert err.http_status == 404


def test_consent_required_403():
    err = ConsentRequired()
    assert err.http_status == 403


def test_dsr_request_invalid_400():
    err = DsrRequestInvalid()
    assert err.http_status == 400


def test_parent_not_authorized_403():
    err = ParentNotAuthorized()
    assert err.http_status == 403


def test_line_binding_not_found_404():
    err = LineBindingNotFound()
    assert err.http_status == 404


def test_bind_code_expired_400():
    err = BindCodeExpired()
    assert err.http_status == 400


def test_portal_data_unavailable_404():
    err = PortalDataUnavailable()
    assert err.http_status == 404


def test_extra_dict_propagates_to_envelope():
    err = BindCodeInvalid(extra={"hint": "請重新掃描 QR Code"})
    assert err.extra == {"hint": "請重新掃描 QR Code"}


# ── 整合測試：raise → envelope handler ────────────────────────────────────────


def _build_test_app() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise-bind-invalid")
    async def raise_bind_invalid():
        raise BindCodeInvalid()

    @app.get("/raise-student-not-found-with-extra")
    async def raise_student_not_found_with_extra():
        raise StudentNotFound(extra={"student_id": 999})

    @app.get("/raise-with-custom-message")
    async def raise_with_custom_message():
        raise StudentNotLinkedToParent("此學生不屬於您")

    return TestClient(app)


def test_envelope_shape_default():
    c = _build_test_app()
    r = c.get("/raise-bind-invalid")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["code"] == "BIND_CODE_INVALID"
    assert body["detail"]["message"] == "綁定碼無效或已過期"
    assert "request_id" in body["detail"]
    assert "X-Request-ID" in r.headers


def test_envelope_shape_with_extra():
    c = _build_test_app()
    r = c.get("/raise-student-not-found-with-extra")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"]["code"] == "STUDENT_NOT_FOUND"
    assert body["detail"]["student_id"] == 999


def test_envelope_shape_with_custom_message():
    c = _build_test_app()
    r = c.get("/raise-with-custom-message")
    assert r.status_code == 403
    body = r.json()
    assert body["detail"]["code"] == "STUDENT_NOT_LINKED_TO_PARENT"
    assert body["detail"]["message"] == "此學生不屬於您"
