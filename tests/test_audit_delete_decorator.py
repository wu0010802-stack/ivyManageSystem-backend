"""Test audit middleware 軟刪/真刪 decorator 與 helpers"""

from types import SimpleNamespace

from utils.audit import mark_soft_delete, mark_hard_delete, _decorate_delete_summary


def _fake_request(method: str, state: dict | None = None):
    state_ns = SimpleNamespace(**(state or {}))
    return SimpleNamespace(method=method, state=state_ns)


def test_mark_soft_delete_sets_summary_and_kind():
    req = _fake_request("PATCH")
    mark_soft_delete(req, "employee", "王小明")
    assert req.state.audit_summary == "軟刪 員工 王小明"
    assert req.state.audit_delete_kind == "soft"


def test_mark_soft_delete_falls_back_to_entity_type_when_unknown():
    req = _fake_request("PATCH")
    mark_soft_delete(req, "unknown_entity", "X-1")
    assert req.state.audit_summary == "軟刪 unknown_entity X-1"


def test_mark_hard_delete_appends_irreversible_marker():
    req = _fake_request("PATCH")
    mark_hard_delete(req, "vendor_payment", "#123")
    assert "(不可復原)" in req.state.audit_summary
    assert req.state.audit_summary == "真刪 廠商付款簽收 #123 (不可復原)"
    assert req.state.audit_delete_kind == "hard"


def test_mark_hard_delete_falls_back_to_entity_type_when_unknown():
    req = _fake_request("PATCH")
    mark_hard_delete(req, "unknown_entity", "X-1")
    assert req.state.audit_summary == "真刪 unknown_entity X-1 (不可復原)"
    assert req.state.audit_delete_kind == "hard"


def test_decorate_http_delete_auto_appends_marker():
    req = _fake_request("DELETE")
    assert _decorate_delete_summary(req, "刪除員工") == "刪除員工 (不可復原)"


def test_decorate_skips_when_delete_kind_already_set():
    req = _fake_request("DELETE", {"audit_delete_kind": "soft"})
    assert _decorate_delete_summary(req, "軟刪 員工 X") == "軟刪 員工 X"


def test_decorate_skips_non_delete_method():
    req = _fake_request("PATCH")
    assert _decorate_delete_summary(req, "修改員工") == "修改員工"


def test_decorate_handles_hard_delete_via_helper():
    """非 HTTP DELETE 但 endpoint 用 mark_hard_delete 標記的情境
    middleware decorate 不應重複加尾綴（因為 helper 已加）。"""
    req = _fake_request("PATCH", {"audit_delete_kind": "hard"})
    assert (
        _decorate_delete_summary(req, "真刪 員工 X (不可復原)")
        == "真刪 員工 X (不可復原)"
    )
