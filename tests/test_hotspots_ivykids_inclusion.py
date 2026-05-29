"""hotspots query UNION RecruitmentVisit ∪ RecruitmentIvykidsRecord

業主決議 §4.7：ivykids 同 hotspot pipeline，但預設 consent NULL → 不上 heatmap；
若 admin 補錄 consent_at → 進 aggregate。
"""

from datetime import datetime

import pytest

from tests.test_recruitment_api import recruitment_session_factory  # noqa: F401
from utils.taipei_time import now_taipei_naive
from api.recruitment.hotspots import _query_address_hotspots
from models.recruitment import RecruitmentIvykidsRecord


def test_ivykids_excluded_when_consent_null(recruitment_session_factory) -> None:
    """來源無 consent 證據 → 預設 NULL → 不上 heatmap"""
    with recruitment_session_factory() as s:
        for i in range(5):
            s.add(
                RecruitmentIvykidsRecord(
                    external_id=f"ext-null-{i}",
                    month="115.05",
                    child_name=f"IK{i}",
                    grade="幼兒",
                    address=f"臺北市內湖區成功路四段100巷{i+1}號",
                    geocoding_consent_at=None,
                )
            )
        s.commit()

    with recruitment_session_factory() as s:
        hotspots, records_with_address, total = _query_address_hotspots(s)

    assert hotspots == [] or len(hotspots) == 0


def test_ivykids_included_when_consent_set(recruitment_session_factory) -> None:
    """admin 補 consent → ivykids 進 heatmap；同 lane 不同號 → 收斂為 1 hotspot"""
    with recruitment_session_factory() as s:
        for i in range(5):
            s.add(
                RecruitmentIvykidsRecord(
                    external_id=f"ext-{i}",
                    month="115.05",
                    child_name=f"IK{i}",
                    grade="幼兒",
                    address=f"臺北市內湖區成功路四段100巷{i+1}號",
                    district="內湖區",
                    geocoding_consent_at=now_taipei_naive(),
                )
            )
        s.commit()

    with recruitment_session_factory() as s:
        hotspots, _records, _total = _query_address_hotspots(s)

    # 5 個 ivykids 同巷 → 1 hotspot
    inhu = [h for h in hotspots if h["district"] == "內湖區"]
    assert len(inhu) == 1, f"truncate UNION 失效: {inhu}"
    assert inhu[0]["visit"] == 5
    assert inhu[0]["address"] == "臺北市內湖區成功路四段100巷"
