"""seed_default_admin DR-safety 行為測試。

涵蓋分流（見 startup/seed.py docstring）：
- DB 已有 admin → no-op（既有部署重啟回歸測試）
- prod + DB 無 admin + 無 ADMIN_INIT_PASSWORD → raise（DR 重建漏設 env 場景）
- prod + DB 無 admin + env 齊備 → 建 admin（DR happy path）
- dev + DB 無 admin + 無 env → fallback admin/admin123 + must_change
"""

import pytest

from models.database import User
from startup.seed import seed_default_admin


def _count_admins(session):
    return session.query(User).filter(User.role == "admin").count()


def test_existing_admin_is_noop_regardless_of_env(test_db_session, monkeypatch):
    """Regression guard: 既有 admin 的部署重啟不應該被新邏輯影響。

    場景：prod + 無 ADMIN_INIT_PASSWORD + DB 已有 admin → 應 no-op、絕不 raise。
    """
    from utils.auth import hash_password

    existing = User(
        employee_id=None,
        username="legacy_admin",
        password_hash=hash_password("seeded-from-history"),
        role="admin",
        permission_names=["*"],
        must_change_password=False,
    )
    test_db_session.add(existing)
    test_db_session.commit()

    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    seed_default_admin()  # no raise

    assert _count_admins(test_db_session) == 1


def test_prod_no_admin_no_env_raises_runtime_error(test_db_session, monkeypatch):
    """DR critical: prod 重建後 DB 無 admin 且漏設 env → fail-fast。

    silent return 會讓容器健康啟動但無人能登入；改為 raise
    讓 healthcheck 持續紅燈，強迫操作者補 env 後重啟。
    """
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    with pytest.raises(RuntimeError, match="ADMIN_INIT_PASSWORD"):
        seed_default_admin()

    assert _count_admins(test_db_session) == 0


def test_prod_no_admin_with_env_creates_admin(test_db_session, monkeypatch):
    """DR happy path: prod 重建後 env 齊備 → 順利建 admin。"""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ADMIN_INIT_USERNAME", "ops_admin")
    monkeypatch.setenv("ADMIN_INIT_PASSWORD", "very-strong-password-xyz")

    seed_default_admin()

    admins = test_db_session.query(User).filter(User.role == "admin").all()
    assert len(admins) == 1
    assert admins[0].username == "ops_admin"
    assert admins[0].must_change_password is False


def test_dev_no_admin_no_env_uses_dev_fallback(test_db_session, monkeypatch):
    """開發環境保留既有 admin/admin123 fallback + must_change_password=True。"""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    seed_default_admin()

    admins = test_db_session.query(User).filter(User.role == "admin").all()
    assert len(admins) == 1
    assert admins[0].username == "admin"
    assert admins[0].must_change_password is True


@pytest.mark.parametrize("env_value", ["staging", "Production", "pruduction", ""])
def test_non_whitelisted_env_no_env_raises_not_weak_fallback(
    test_db_session, monkeypatch, env_value
):
    """C38：非白名單 ENV（staging/typo/空字串）漏設 ADMIN_INIT_PASSWORD 不得
    fallback 弱密碼 admin/admin123，必須 fail-fast raise。

    舊邏輯只在 is_production() 才 raise，其餘（含 staging/typo/空）落入弱密碼
    fallback——等同在類正式環境靜默建立 admin/admin123 後門。
    """
    monkeypatch.setenv("ENV", env_value)
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    with pytest.raises(RuntimeError):
        seed_default_admin()

    assert _count_admins(test_db_session) == 0


def test_unset_env_no_env_raises_not_weak_fallback(test_db_session, monkeypatch):
    """C38：未設 ENV（model_fields_set 不含 env）亦視為未配置 dev → fail-fast。"""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    with pytest.raises(RuntimeError):
        seed_default_admin()

    assert _count_admins(test_db_session) == 0


@pytest.mark.parametrize("env_value", ["dev", "local", "test"])
def test_whitelisted_dev_env_still_allows_fallback(
    test_db_session, monkeypatch, env_value
):
    """C38：白名單 dev/local/test 維持弱密碼 fallback（本地開發體驗不變）。"""
    monkeypatch.setenv("ENV", env_value)
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_INIT_USERNAME", raising=False)

    seed_default_admin()

    admins = test_db_session.query(User).filter(User.role == "admin").all()
    assert len(admins) == 1
    assert admins[0].username == "admin"
    assert admins[0].must_change_password is True
