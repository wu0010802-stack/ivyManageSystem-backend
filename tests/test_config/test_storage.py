from pathlib import Path
import pytest
from config.storage import StorageSettings


def test_defaults(monkeypatch):
    for var in (
        "STORAGE_BACKEND",
        "STORAGE_ROOT",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_STORAGE_SIGNED_URL_TTL",
        "GROWTH_REPORT_ROOT",
        "GROWTH_REPORT_MAX_BYTES",
    ):
        monkeypatch.delenv(var, raising=False)
    s = StorageSettings()
    assert s.backend == "local"
    assert s.root is None  # env 沒設則 None，由 utils/storage.py 決定 fallback
    assert s.supabase_url is None
    assert s.supabase_service_role_key is None
    assert s.supabase_signed_url_ttl == 300
    assert s.growth_report_root == Path("./growth_reports")
    assert s.growth_report_max_bytes == 5_242_880


def test_supabase_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://xxx.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJ...")
    monkeypatch.setenv("SUPABASE_STORAGE_SIGNED_URL_TTL", "7200")
    s = StorageSettings()
    assert s.backend == "supabase"
    assert s.supabase_url == "https://xxx.supabase.co"
    assert s.supabase_signed_url_ttl == 7200
