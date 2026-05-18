"""tests/test_sentry_scrubber.py — Sentry PII scrubber 與 init 邏輯單元測試。

驗證範圍：
- _sanitize_url: URL path 中段純數字 → `:id`
- _scrub_mapping: 金流 / 個資 / 幼教 / 醫療 / 認證 欄位被遮罩；非 PII 不動；大小寫不敏感；遞迴
- _scrub_event: request/transaction/breadcrumbs/contexts 全鏈路遮罩
- init_sentry: DSN 缺 → False（no-op）；有 DSN → 呼叫 sdk.init（mock）並傳對參數
"""

import hashlib

import sentry_sdk

from utils.sentry_init import (
    _hash_user_id,
    _sanitize_url,
    _scrub_breadcrumb,
    _scrub_event,
    _scrub_mapping,
    _scrub_query_string,
    init_sentry,
)

# ---------------------------------------------------------------------------
# _sanitize_url
# ---------------------------------------------------------------------------


class TestSanitizeUrl:
    def test_replaces_single_id(self):
        assert _sanitize_url("/api/students/123") == "/api/students/:id"

    def test_replaces_multiple_ids(self):
        assert (
            _sanitize_url("/api/students/123/measurements/45")
            == "/api/students/:id/measurements/:id"
        )

    def test_leaves_path_templates_alone(self):
        # FastAPI endpoint style 已是 template 格式，不該被當作 id
        assert (
            _sanitize_url("/api/students/{student_id}") == "/api/students/{student_id}"
        )

    def test_leaves_year_month_alone_in_query(self):
        # query string 內的數字不替換（regex 限定 `/數字`）
        assert (
            _sanitize_url("/api/salary/preview?year=2026&month=5")
            == "/api/salary/preview?year=2026&month=5"
        )

    def test_id_followed_by_query(self):
        assert (
            _sanitize_url("/api/employees/77?include=salary")
            == "/api/employees/:id?include=salary"
        )

    def test_empty_returns_empty(self):
        assert _sanitize_url("") == ""

    def test_non_string_passthrough(self):
        # 防呆：非 string 不該炸（Sentry payload 可能塞奇怪值進來）
        assert _sanitize_url(None) is None  # type: ignore[arg-type]

    def test_query_string_pii_filtered(self):
        # regression：query 內 PII key 過去完全繞過 scrubber，現在應遮值。
        result = _sanitize_url("/api/students/search?phone=0912345678")
        assert "0912345678" not in result
        assert "phone=" in result and "Filtered" in result

    def test_query_string_keeps_non_pii(self):
        # year/month 是業務必要 metric，必須保留（exempt 風險 — 過度遮會破壞功能）
        assert (
            _sanitize_url("/api/salary/preview?year=2026&month=5")
            == "/api/salary/preview?year=2026&month=5"
        )

    def test_query_string_mixed_pii_and_metric(self):
        result = _sanitize_url(
            "/api/students/search?email=alice@example.com&id_number=A123456789&year=2026"
        )
        assert "alice@example.com" not in result
        assert "A123456789" not in result
        assert "year=2026" in result  # exempt 保留

    def test_path_id_with_query_pii(self):
        result = _sanitize_url("/api/students/123?phone=0912345678")
        assert result.startswith("/api/students/:id?")
        assert "0912345678" not in result

    def test_full_url_with_query_pii(self):
        result = _sanitize_url("https://x.com/api/students/123?phone=0912&include=name")
        assert result.startswith("https://x.com/api/students/:id?")
        assert "0912" not in result
        assert "include=name" in result


# ---------------------------------------------------------------------------
# _scrub_mapping
# ---------------------------------------------------------------------------


class TestScrubMapping:
    def test_finance_keys_filtered(self):
        result = _scrub_mapping(
            {
                "base_salary": 50000,
                "insured_amount": 45800,
                "dependents": 2,
                "bonus_amount": 1000,
                "bank_account": "0012345678",
                "name": "Alice",  # 非 PII 保留
            }
        )
        assert result["base_salary"] == "[Filtered]"
        assert result["insured_amount"] == "[Filtered]"
        assert result["dependents"] == "[Filtered]"
        assert result["bonus_amount"] == "[Filtered]"
        assert result["bank_account"] == "[Filtered]"
        assert result["name"] == "Alice"

    def test_identity_keys_filtered(self):
        result = _scrub_mapping(
            {
                "id_number": "A123456789",
                "passport_id": "X1234",
                "phone": "0912345678",
                "mobile": "0987654321",
                "email": "x@y.com",
                "address": "台北市",
                "home_address": "信義區",
            }
        )
        for k in (
            "id_number",
            "passport_id",
            "phone",
            "mobile",
            "email",
            "address",
            "home_address",
        ):
            assert result[k] == "[Filtered]", f"{k} 沒被遮"

    def test_child_and_medical_keys_filtered(self):
        result = _scrub_mapping(
            {
                "child_name": "小明",
                "student_name": "小華",
                "parent_name": "王太太",
                "guardian_phone": "0912",
                "emergency_contact_name": "阿嬤",
                "birthday": "2020-01-01",
                "medication": "Tylenol",
                "dosage": "5ml",
                "allergy": "花生",
                "iep_plan": "X",
                "diagnosis": "ADHD",
                "growth_record": {},
                "measurement_height": 100,
                "weight_kg": 15,
            }
        )
        for k in (
            "child_name",
            "student_name",
            "parent_name",
            "guardian_phone",
            "emergency_contact_name",
            "birthday",
            "medication",
            "dosage",
            "allergy",
            "iep_plan",
            "diagnosis",
            "growth_record",
            "measurement_height",
            "weight_kg",
        ):
            assert result[k] == "[Filtered]", f"{k} 沒被遮"

    def test_auth_keys_filtered(self):
        result = _scrub_mapping(
            {
                "password": "x",
                "Authorization": "Bearer y",
                "Cookie": "session=z",
                "jwt_token": "abc",
                "refresh_token": "rrr",
                "access_token": "aaa",
                "api_key": "kkk",
                "line_user_id": "U123",
                "liff_id": "L456",
            }
        )
        for k in (
            "password",
            "Authorization",
            "Cookie",
            "jwt_token",
            "refresh_token",
            "access_token",
            "api_key",
            "line_user_id",
            "liff_id",
        ):
            assert result[k] == "[Filtered]", f"{k} 沒被遮"

    def test_case_insensitive(self):
        result = _scrub_mapping({"SALARY": 1, "Phone": "X", "Authorization": "Y"})
        assert result["SALARY"] == "[Filtered]"
        assert result["Phone"] == "[Filtered]"
        assert result["Authorization"] == "[Filtered]"

    def test_nested_dict_recursive(self):
        result = _scrub_mapping(
            {"meta": {"id_number": "A1", "ok": "yes"}, "name": "Alice"}
        )
        assert result["meta"]["id_number"] == "[Filtered]"
        assert result["meta"]["ok"] == "yes"
        assert result["name"] == "Alice"

    def test_list_of_dicts(self):
        result = _scrub_mapping([{"password": "x"}, {"salary": 1}, {"normal": "y"}])
        assert result[0]["password"] == "[Filtered]"
        assert result[1]["salary"] == "[Filtered]"
        assert result[2]["normal"] == "y"

    def test_primitives_unchanged(self):
        assert _scrub_mapping("hello") == "hello"
        assert _scrub_mapping(42) == 42
        assert _scrub_mapping(None) is None

    def test_substring_match_not_just_exact(self):
        # 業務上會有 employee_salary / monthly_base_salary 等延伸欄位
        result = _scrub_mapping(
            {
                "employee_salary_after_tax": 40000,
                "monthly_base_salary": 50000,
                "parent_email": "x@y.com",
            }
        )
        assert result["employee_salary_after_tax"] == "[Filtered]"
        assert result["monthly_base_salary"] == "[Filtered]"
        assert result["parent_email"] == "[Filtered]"

    def test_exempt_fields_survive(self):
        """系統 / metric 欄位即便子字串命中 denylist 也不該被遮（prod debug 需要）。

        例：ip_address 含 'address' / health_check 含 'health' / email_template 含 'email' /
        growth_funnel_count 含 'growth' / measurement_unit 含 'measurement'。
        """
        result = _scrub_mapping(
            {
                "ip_address": "1.2.3.4",
                "request_ip_addr_v6": "::1",
                "health_check": "ok",
                "healthcheck_status": "green",
                "email_template_id": 5,
                "email_subject": "Welcome",
                "growth_funnel_count": 30,
                "growth_rate": 0.15,
                "measurement_unit": "kg",
                "measurement_type": "weight",
            }
        )
        for k, expected in result.items():
            assert expected != "[Filtered]", f"{k} 被誤遮：應由 exempt 放行"

    def test_personal_growth_still_filtered_despite_exempt(self):
        """exempt 只放行系統 metric；個人 growth_record / measurement_value 仍要遮。"""
        result = _scrub_mapping(
            {
                "growth_record": {"data": "..."},
                "growth_data": "...",
                "measurement_value": 100,
                "measurement_height": 95,
            }
        )
        assert result["growth_record"] == "[Filtered]"
        assert result["growth_data"] == "[Filtered]"
        assert result["measurement_value"] == "[Filtered]"
        assert result["measurement_height"] == "[Filtered]"


# ---------------------------------------------------------------------------
# _scrub_event
# ---------------------------------------------------------------------------


class TestScrubEvent:
    def test_request_url_sanitized(self):
        ev = {"request": {"url": "https://x.com/api/students/123/iep"}}
        result = _scrub_event(ev)
        assert result["request"]["url"] == "https://x.com/api/students/:id/iep"

    def test_request_headers_scrubbed(self):
        ev = {
            "request": {
                "url": "/x",
                "headers": {
                    "Authorization": "Bearer xxx",
                    "Cookie": "session=yyy",
                    "User-Agent": "okay",
                },
            }
        }
        result = _scrub_event(ev)
        assert result["request"]["headers"]["Authorization"] == "[Filtered]"
        assert result["request"]["headers"]["Cookie"] == "[Filtered]"
        assert result["request"]["headers"]["User-Agent"] == "okay"

    def test_request_data_scrubbed(self):
        ev = {
            "request": {
                "url": "/x",
                "data": {
                    "password": "x",
                    "child_name": "小明",
                    "phone": "0912",
                    "title": "正常",
                },
            }
        }
        result = _scrub_event(ev)
        assert result["request"]["data"]["password"] == "[Filtered]"
        assert result["request"]["data"]["child_name"] == "[Filtered]"
        assert result["request"]["data"]["phone"] == "[Filtered]"
        assert result["request"]["data"]["title"] == "正常"

    def test_transaction_sanitized(self):
        ev = {"transaction": "GET /api/students/123"}
        result = _scrub_event(ev)
        assert result["transaction"] == "GET /api/students/:id"

    def test_breadcrumb_url_and_data_scrubbed(self):
        ev = {
            "breadcrumbs": {
                "values": [
                    {
                        "message": "fetch /api/employees/77",
                        "data": {"phone": "0912"},
                    }
                ]
            }
        }
        result = _scrub_event(ev)
        crumb = result["breadcrumbs"]["values"][0]
        assert crumb["message"] == "fetch /api/employees/:id"
        assert crumb["data"]["phone"] == "[Filtered]"

    def test_user_section_scrubbed(self):
        ev = {"user": {"id": 1, "email": "x@y.com", "username": "alice"}}
        result = _scrub_event(ev)
        assert result["user"]["email"] == "[Filtered]"
        # user.id 對映 employees.id / parents.id —— hash 化避免直連個人
        expected_hash = hashlib.blake2b(b"1", digest_size=4).hexdigest()
        assert result["user"]["id"] == expected_hash
        assert result["user"]["username"] == "alice"

    def test_user_id_string_hashed(self):
        ev = {"user": {"id": "U-12345"}}
        result = _scrub_event(ev)
        expected = hashlib.blake2b(b"U-12345", digest_size=4).hexdigest()
        assert result["user"]["id"] == expected
        assert len(result["user"]["id"]) == 8  # blake2b digest_size=4 → 8 hex

    def test_user_id_none_passthrough(self):
        ev = {"user": {"id": None, "email": "x@y.com"}}
        result = _scrub_event(ev)
        assert result["user"]["id"] is None
        assert result["user"]["email"] == "[Filtered]"

    def test_request_query_string_pii_filtered(self):
        # request.query_string 過去送字串原樣進 Sentry —— 現在 parse 後遮 PII
        ev = {"request": {"url": "/x", "query_string": "phone=0912&year=2026"}}
        result = _scrub_event(ev)
        qs = result["request"]["query_string"]
        assert "0912" not in qs
        assert "year=2026" in qs

    def test_extra_and_contexts_scrubbed(self):
        ev = {
            "extra": {"base_salary": 50000, "note": "ok"},
            "contexts": {"runtime": {"version": "3.13"}, "user": {"phone": "0912"}},
        }
        result = _scrub_event(ev)
        assert result["extra"]["base_salary"] == "[Filtered]"
        assert result["extra"]["note"] == "ok"
        assert result["contexts"]["runtime"]["version"] == "3.13"
        assert result["contexts"]["user"]["phone"] == "[Filtered]"

    def test_non_dict_event_passthrough(self):
        # Sentry SDK 偶有送 non-dict（極罕見）；不該炸
        assert _scrub_event("not a dict") == "not a dict"  # type: ignore[arg-type]


class TestScrubBreadcrumb:
    def test_url_in_message_sanitized(self):
        crumb = {"message": "GET /api/fees/records/789", "category": "http"}
        result = _scrub_breadcrumb(crumb)
        assert result["message"] == "GET /api/fees/records/:id"

    def test_data_pii_filtered(self):
        crumb = {"category": "http", "data": {"id_number": "A1", "url": "/x/1"}}
        result = _scrub_breadcrumb(crumb)
        assert result["data"]["id_number"] == "[Filtered]"


# ---------------------------------------------------------------------------
# _scrub_query_string
# ---------------------------------------------------------------------------


class TestScrubQueryString:
    def test_pii_value_filtered(self):
        result = _scrub_query_string("phone=0912345678&name=Alice")
        assert "0912345678" not in result
        assert "name=Alice" in result

    def test_non_pii_preserved(self):
        assert _scrub_query_string("year=2026&month=5") == "year=2026&month=5"

    def test_dict_input_uses_scrub_mapping(self):
        assert _scrub_query_string({"phone": "0912", "ok": "yes"}) == {
            "phone": "[Filtered]",
            "ok": "yes",
        }

    def test_empty_string_passthrough(self):
        assert _scrub_query_string("") == ""


# ---------------------------------------------------------------------------
# _hash_user_id
# ---------------------------------------------------------------------------


class TestHashUserId:
    def test_int_hashed(self):
        assert _hash_user_id(1) == hashlib.blake2b(b"1", digest_size=4).hexdigest()

    def test_string_hashed(self):
        assert (
            _hash_user_id("U-99") == hashlib.blake2b(b"U-99", digest_size=4).hexdigest()
        )

    def test_none_passthrough(self):
        assert _hash_user_id(None) is None

    def test_empty_string_passthrough(self):
        assert _hash_user_id("") == ""

    def test_deterministic(self):
        # Sentry issue grouping 依賴同 id 永遠 hash 到同字串
        assert _hash_user_id(42) == _hash_user_id(42)

    def test_different_ids_yield_different_hashes(self):
        assert _hash_user_id(1) != _hash_user_id(2)


# ---------------------------------------------------------------------------
# init_sentry
# ---------------------------------------------------------------------------


class TestInitSentry:
    def test_noop_without_dsn(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        assert init_sentry() is False

    def test_noop_when_dsn_blank(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "   ")
        assert init_sentry() is False

    def test_init_passes_expected_kwargs(self, monkeypatch):
        captured: dict = {}

        def fake_init(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(sentry_sdk, "init", fake_init)
        monkeypatch.setenv("SENTRY_DSN", "https://pub@o0.ingest.sentry.io/0")
        monkeypatch.setenv("SENTRY_ENVIRONMENT", "test-env")
        monkeypatch.setenv("SENTRY_RELEASE", "ivy-backend@x.y.z")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")

        assert init_sentry() is True
        assert captured["dsn"] == "https://pub@o0.ingest.sentry.io/0"
        assert captured["environment"] == "test-env"
        assert captured["release"] == "ivy-backend@x.y.z"
        assert captured["traces_sample_rate"] == 0.25
        assert captured["send_default_pii"] is False
        assert callable(captured["before_send"])
        assert callable(captured["before_breadcrumb"])
        # before_send 確實是我們的 scrubber：跑一條典型 event 看是否被遮
        scrubbed = captured["before_send"](
            {"request": {"url": "/api/x/1", "data": {"password": "x"}}}, None
        )
        assert scrubbed["request"]["url"] == "/api/x/:id"
        assert scrubbed["request"]["data"]["password"] == "[Filtered]"

    def test_invalid_traces_rate_falls_back_to_default(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setenv("SENTRY_DSN", "https://pub@o0.ingest.sentry.io/0")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "not-a-number")
        assert init_sentry() is True
        assert captured["traces_sample_rate"] == 0.1
