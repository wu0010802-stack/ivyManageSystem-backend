import pytest

from scripts.seedgen.guard import assert_dev_db, GuardError


def test_localhost_ivymanagement_ok():
    assert_dev_db(
        "postgresql://yilunwu@localhost:5432/ivymanagement",
        env="development",
        allow_non_dev=False,
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.zeabur.internal:5432/ivymanagement",
        "postgresql://u:p@aws-0-x.pooler.supabase.com:6543/postgres",
        "postgresql://yilunwu@localhost:5432/ivymanagement?sslmode=require",
    ],
)
def test_remote_rejected(url):
    with pytest.raises(GuardError):
        assert_dev_db(url, env="development", allow_non_dev=False)


def test_production_env_rejected():
    with pytest.raises(GuardError):
        assert_dev_db(
            "postgresql://yilunwu@localhost:5432/ivymanagement",
            env="production",
            allow_non_dev=False,
        )


def test_override():
    assert_dev_db("postgresql://x@remote/db", env="production", allow_non_dev=True)
