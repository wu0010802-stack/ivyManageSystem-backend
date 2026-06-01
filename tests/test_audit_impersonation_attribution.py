from models.audit import AuditLog


def test_auditlog_has_impersonation_columns():
    cols = AuditLog.__table__.columns.keys()
    assert "impersonated_by" in cols
    assert "impersonated_by_name" in cols
