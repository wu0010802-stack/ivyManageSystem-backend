"""ParentAssistantService 測試：載入 + mtime cache。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def temp_faq(tmp_path, monkeypatch):
    """產生臨時 FAQ 檔，把 service 的 _path 指向它。"""
    from services import parent_assistant_service as mod

    faq_path = tmp_path / "parent_faq.json"
    faq_path.write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "updated_at": "2026-05-16",
                "categories": [
                    {"id": "leave", "label": "請假", "icon": "x", "color": "#000"}
                ],
                "items": [
                    {
                        "id": "leave-1",
                        "category": "leave",
                        "question": "Q?",
                        "keywords": [],
                        "answer": "A",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 重設 class-level cache 並指到臨時檔
    mod.ParentAssistantService._cache = None
    mod.ParentAssistantService._cached_mtime = None
    monkeypatch.setattr(mod.ParentAssistantService, "_path", faq_path)
    return faq_path


def test_first_call_loads_file(temp_faq):
    from services.parent_assistant_service import ParentAssistantService

    data = ParentAssistantService.get_faq()
    assert data["version"] == "1.0.0"
    assert data["items"][0]["id"] == "leave-1"


def test_repeated_call_does_not_reload_when_mtime_unchanged(temp_faq, monkeypatch):
    from services import parent_assistant_service as mod

    mod.ParentAssistantService.get_faq()  # warm cache
    call_count = {"n": 0}
    original_open = Path.open

    def spy_open(self, *args, **kwargs):
        if self == temp_faq:
            call_count["n"] += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)
    mod.ParentAssistantService.get_faq()
    assert call_count["n"] == 0, "mtime 未變不應重讀"


def test_reload_when_mtime_changes(temp_faq):
    from services.parent_assistant_service import ParentAssistantService

    ParentAssistantService.get_faq()
    # 改檔 + 推進 mtime
    new_payload = {
        "version": "2.0.0",
        "updated_at": "2026-05-17",
        "categories": [],
        "items": [],
    }
    temp_faq.write_text(json.dumps(new_payload), encoding="utf-8")
    new_mtime = temp_faq.stat().st_mtime + 1
    os.utime(temp_faq, (new_mtime, new_mtime))

    data = ParentAssistantService.get_faq()
    assert data["version"] == "2.0.0"


def test_invalid_json_raises(temp_faq):
    from services.parent_assistant_service import ParentAssistantService

    temp_faq.write_text("not json", encoding="utf-8")
    new_mtime = temp_faq.stat().st_mtime + 1
    os.utime(temp_faq, (new_mtime, new_mtime))

    with pytest.raises(json.JSONDecodeError):
        ParentAssistantService.get_faq()
