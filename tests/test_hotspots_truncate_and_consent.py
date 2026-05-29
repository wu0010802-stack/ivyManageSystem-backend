"""hotspots GROUP BY truncated address + consent filter

驗證 _query_address_hotspots:
1. truncate 真實生效（同巷不同戶收斂為 1 hotspot）
2. consent_at IS NULL 的 visit 不進 aggregate
"""

from datetime import datetime

import pytest

from tests.test_recruitment_api import (  # noqa: F401
    recruitment_session_factory,
)
from utils.taipei_time import now_taipei_naive
from api.recruitment.hotspots import _query_address_hotspots
from models.recruitment import RecruitmentVisit


def test_hotspots_groups_by_truncated_address(recruitment_session_factory) -> None:
    """5 戶不同門牌號同巷 → 應收斂為 1 hotspot visit=5（advisor catch:
    不能只測 total，需測 hotspot 數=1）"""
    with recruitment_session_factory() as s:
        for n in range(5):
            s.add(
                RecruitmentVisit(
                    month="115.05",
                    child_name=f"K{n}",
                    grade="幼兒",
                    address=f"臺北市文山區興隆路四段30巷{n+1}號",
                    geocoding_consent_at=now_taipei_naive(),
                )
            )
        s.commit()

    with recruitment_session_factory() as s:
        hotspots, records_with_address, total = _query_address_hotspots(s)

    assert len(hotspots) == 1, f"truncate 失效：應 1 個 hotspot 實際 {len(hotspots)} 個"
    assert hotspots[0]["visit"] == 5, f"visit count 不對：{hotspots[0]}"
    assert (
        hotspots[0]["address"] == "臺北市文山區興隆路四段30巷"
    ), f"truncated address 不對：{hotspots[0]['address']}"


def test_hotspots_excludes_null_consent(recruitment_session_factory) -> None:
    """consent_at = NULL 的 visit 不進 hotspot pipeline"""
    with recruitment_session_factory() as s:
        s.add(
            RecruitmentVisit(
                month="115.05",
                child_name="With",
                grade="幼兒",
                address="臺北市中山區某路1號",
                geocoding_consent_at=now_taipei_naive(),
            )
        )
        s.add(
            RecruitmentVisit(
                month="115.05",
                child_name="Without",
                grade="幼兒",
                address="臺北市中山區某路1號",
                geocoding_consent_at=None,
            )
        )
        s.commit()

    with recruitment_session_factory() as s:
        hotspots, _, _ = _query_address_hotspots(s)

    aggregated = sum(h["visit"] for h in hotspots)
    assert aggregated == 1, f"無 consent visit 不應入 aggregate, 實際: {aggregated}"
