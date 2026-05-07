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
