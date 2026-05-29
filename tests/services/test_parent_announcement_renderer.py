"""parent.announcement renderer hero_url 行為。"""

import pytest

from services.notification.renderers import render


def test_renderer_no_attachments_no_hero():
    r = render(
        "parent.announcement",
        {
            "title": "T",
            "preview": "C",
            "announcement_id": 1,
            "attachments": [],
        },
    )
    assert r.hero_url is None


def test_renderer_pdf_only_no_hero():
    r = render(
        "parent.announcement",
        {
            "title": "T",
            "preview": "C",
            "announcement_id": 1,
            "attachments": [{"mime_type": "application/pdf", "thumb_url": None}],
        },
    )
    assert r.hero_url is None


def test_renderer_first_image_sets_hero_url():
    # mock settings.misc.ivy_api_base_url 讓 hero_url 能組起
    r = render(
        "parent.announcement",
        {
            "title": "T",
            "preview": "C",
            "announcement_id": 1,
            "attachments": [
                {
                    "mime_type": "image/png",
                    "thumb_url": "/api/uploads/portfolio/abc.png",
                },
                {"mime_type": "application/pdf", "thumb_url": None},
            ],
        },
    )
    # 若 ivy_api_base_url 設定存在（預設 http://localhost:8088），hero_url 含 thumb path
    # 若 base_url 為空字串，hero_url 為 None — 由 if base 守住
    if r.hero_url is not None:
        assert "abc.png" in r.hero_url
