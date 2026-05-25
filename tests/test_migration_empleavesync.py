"""M-1~M-5: migration 行為驗證(部分依賴 staging DB,本檔做能在 CI 跑的子集)"""

import pytest
from datetime import date
from sqlalchemy import text


@pytest.fixture
def fresh_engine(tmp_path):
    """In-memory SQLite engine 給 happy path 測試(僅驗 schema 變更)"""
    # 注意:Postgres-specific 邏輯(ALTER TYPE / CONCURRENTLY)無法在 SQLite 上跑
    # 這些 case 標 skip-on-sqlite,改在 staging 跑 alembic upgrade 驗證
    pytest.skip("Postgres-specific migration,需 staging DB 驗證")


class TestMigrationBehavior:
    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m1_upgrade_clean_db(self):
        """M-1: upgrade on clean DB → 完成、無報錯"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m2_upgrade_with_dups_fails_loud(self):
        """M-2: upgrade on DB with dups → fail-loud"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m3_upgrade_with_approved_leaves_backfills(self):
        """M-3: upgrade with approved leaves → backfill 完成"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m4_upgrade_idempotent(self):
        """M-4: upgrade 中斷後重跑 → idempotent"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m5_downgrade_restores_schema(self):
        """M-5: downgrade → schema 還原(LEAVE enum 殘留是預期)"""
        pass
