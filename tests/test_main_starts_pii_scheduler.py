"""Smoke test: main.py wires pii_retention_scheduler correctly."""

import importlib


def test_main_imports_and_wires_pii_scheduler():
    """smoke: main 模組 import 不爆、pii_retention_scheduler 公開 API 存在。"""
    # Import main to ensure no syntax errors in scheduler wiring
    main_mod = importlib.import_module("main")
    assert main_mod is not None

    # Verify pii_retention_scheduler has required public API
    from services import pii_retention_scheduler as pii

    assert hasattr(pii, "run_pii_retention_scheduler")
    assert hasattr(pii, "scheduler_enabled")
    assert callable(pii.run_pii_retention_scheduler)
    assert callable(pii.scheduler_enabled)
