"""驗證 utils/audit.py 的 ENTITY_PATTERNS 對特定路徑會 match 到正確 entity_type。

主要回歸點：approval-settings policy 異動必須被 AuditMiddleware 偵測。
原本 PUT /api/approval-settings/policies 完全未列入 ENTITY_PATTERNS，導致
admin 改規則 → 自批 → 改回，全程零 audit（audit 2026-05-07 P0 #13）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.audit import ENTITY_LABELS, _parse_entity_type


class TestApprovalPolicyAuditCoverage:
    def test_approval_settings_policies_put_mapped(self):
        """PUT /api/approval-settings/policies → entity_type=approval_policy。"""
        assert (
            _parse_entity_type("/api/approval-settings/policies") == "approval_policy"
        )

    def test_approval_settings_logs_get_mapped(self):
        """GET 也 mapping 到 approval_policy（middleware 端會以 method 過濾 GET）。"""
        assert _parse_entity_type("/api/approval-settings/logs") == "approval_policy"

    def test_approval_policy_entity_label_present(self):
        """ENTITY_LABELS 必須有 approval_policy 中文 label，供 audit-logs/meta 端點。"""
        assert ENTITY_LABELS.get("approval_policy") == "審核流程設定"


class TestUnaffectedEntityPatterns:
    """確認加入新 pattern 沒有打亂既有 first-match 順序。"""

    def test_config_titles_still_specific(self):
        assert _parse_entity_type("/api/config/titles") == "job_title"

    def test_config_general_still_works(self):
        assert _parse_entity_type("/api/config/something") == "config"

    def test_employees_still_employee(self):
        assert _parse_entity_type("/api/employees/123") == "employee"

    def test_unmatched_path_returns_none(self):
        assert _parse_entity_type("/api/health") is None


class TestYearEndAuditCoverage:
    """bug sweep 2026-05-16 P0-1c：年終獎金結算三模組必須有 audit 規則。

    缺規則 = AuditMiddleware 不會落 audit_logs（誰 finalize、誰加 special_bonus 不可追）。
    """

    def test_year_end_cycles_post_mapped(self):
        assert _parse_entity_type("/api/year_end/cycles") == "year_end_cycle"

    def test_year_end_cycle_org_settings_mapped(self):
        """POST /api/year_end/cycles/{id}/org_settings 仍歸 year_end_cycle。"""
        assert (
            _parse_entity_type("/api/year_end/cycles/5/org_settings")
            == "year_end_cycle"
        )

    def test_year_end_special_bonuses_more_specific_than_cycles(self):
        """special_bonuses 必須在 cycles 之前（first-match wins）。"""
        assert (
            _parse_entity_type("/api/year_end/cycles/5/special_bonuses")
            == "year_end_special_bonus"
        )

    def test_year_end_settlements_sign_supervisor_mapped(self):
        assert (
            _parse_entity_type("/api/year_end/settlements/12/sign_supervisor")
            == "year_end_settlement"
        )

    def test_year_end_settlements_finalize_mapped(self):
        assert (
            _parse_entity_type("/api/year_end/settlements/12/finalize")
            == "year_end_settlement"
        )

    def test_year_end_entity_labels_present(self):
        """ENTITY_LABELS 必須有對應中文 label，供 audit-logs/meta 端點。"""
        assert ENTITY_LABELS.get("year_end_cycle") == "年終週期"
        assert ENTITY_LABELS.get("year_end_settlement") == "年終結算"
        assert ENTITY_LABELS.get("year_end_special_bonus") == "年終特別獎金"


class TestPortfolioGranularAuditCoverage:
    """audit 2026-05-25：portfolio 子模組寫操作要走細粒度 entity_type，
    不再混進 student 主檔 entity_type=student。

    timeline / attachments 兩支 GET 由 endpoint 自身呼叫 write_explicit_audit
    保留 entity_type='student'（跨模組聚合稽核 F-V6-03），不在此分流。
    """

    def test_milestones_write_mapped_to_portfolio_milestone(self):
        assert (
            _parse_entity_type("/api/students/123/milestones") == "portfolio_milestone"
        )

    def test_milestones_with_subpath_still_portfolio_milestone(self):
        assert (
            _parse_entity_type("/api/students/123/milestones/55")
            == "portfolio_milestone"
        )

    def test_measurements_mapped_to_student_measurement(self):
        assert (
            _parse_entity_type("/api/students/7/measurements") == "student_measurement"
        )
        assert (
            _parse_entity_type("/api/students/7/measurements/12")
            == "student_measurement"
        )

    def test_observations_mapped_to_student_observation(self):
        assert (
            _parse_entity_type("/api/students/9/observations") == "student_observation"
        )

    def test_growth_reports_mapped_to_student_growth_report(self):
        assert (
            _parse_entity_type("/api/students/3/growth-reports")
            == "student_growth_report"
        )
        assert (
            _parse_entity_type("/api/students/3/growth-reports/77")
            == "student_growth_report"
        )

    def test_students_root_still_student(self):
        """fallback 仍要保留 — /api/students/{id} 本體與其他未列舉子路徑都是 student。"""
        assert _parse_entity_type("/api/students/123") == "student"
        assert _parse_entity_type("/api/students/123/timeline") == "student"
        assert _parse_entity_type("/api/students/123/attachments") == "student"

    def test_portfolio_entity_labels_present(self):
        assert ENTITY_LABELS.get("portfolio_milestone") == "學生里程碑"
        assert ENTITY_LABELS.get("student_observation") == "學生觀察紀錄"
        assert ENTITY_LABELS.get("student_growth_report") == "學生成長報告"
        assert ENTITY_LABELS.get("portfolio_download") == "家長下載 portfolio 檔案"


class TestContactBookTemplateAuditCoverage:
    """audit 2026-05-25：教師端 contact-book/templates 與 entry 業務不同，分流。"""

    def test_templates_mapped_to_contact_book_template(self):
        assert (
            _parse_entity_type("/api/portal/contact-book/templates")
            == "contact_book_template"
        )

    def test_templates_with_id_still_template(self):
        assert (
            _parse_entity_type("/api/portal/contact-book/templates/5/promote")
            == "contact_book_template"
        )

    def test_contact_book_entry_unchanged(self):
        """/api/portal/contact-book 本體仍歸 contact_book_entry。"""
        assert _parse_entity_type("/api/portal/contact-book") == "contact_book_entry"
        assert (
            _parse_entity_type("/api/portal/contact-book/12/publish")
            == "contact_book_entry"
        )

    def test_contact_book_template_label_present(self):
        assert ENTITY_LABELS.get("contact_book_template") == "聯絡簿範本"


class TestReadDedup:
    """audit 2026-05-25：write_explicit_audit(dedup=True) 同 (user, entity_type,
    entity_id) 60s 內只記第一筆，避免家長端 list endpoint 量爆。"""

    def setup_method(self):
        from utils import audit as audit_module

        audit_module._audit_read_cache.clear()

    def test_same_key_within_window_dedups(self):
        from utils.audit import _should_audit_read

        assert _should_audit_read(1, "contact_book_entry", "5") is True
        assert _should_audit_read(1, "contact_book_entry", "5") is False
        assert _should_audit_read(1, "contact_book_entry", "5") is False

    def test_different_entity_id_not_deduped(self):
        from utils.audit import _should_audit_read

        assert _should_audit_read(1, "contact_book_entry", "5") is True
        assert _should_audit_read(1, "contact_book_entry", "6") is True

    def test_different_user_not_deduped(self):
        from utils.audit import _should_audit_read

        assert _should_audit_read(1, "contact_book_entry", "5") is True
        assert _should_audit_read(2, "contact_book_entry", "5") is True

    def test_different_entity_type_not_deduped(self):
        """同學生同 ID 但不同 entity_type 不互相壓制。"""
        from utils.audit import _should_audit_read

        assert _should_audit_read(1, "student_measurement", "5") is True
        assert _should_audit_read(1, "student_observation", "5") is True

    def test_anon_user_keyed_distinctly(self):
        from utils.audit import _should_audit_read

        assert _should_audit_read(None, "student_measurement", "5") is True
        assert _should_audit_read(None, "student_measurement", "5") is False
