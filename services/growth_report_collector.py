"""Pure functions to aggregate data for a student growth report.

無 DB 依賴；接收 ORM rows / dicts，回傳純資料結構供 PDF 生成器使用。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

PRESENT_STATUS = "出席"


def summarize_attendance(records: Iterable[dict]) -> dict:
    """Summarize attendance records into counts + present_rate.

    Records: iterable of {"date": date, "status": str}.
    """
    records = list(records)
    total = len(records)
    if total == 0:
        return {
            "total_days": 0,
            "present_days": 0,
            "leave_days": 0,
            "sick_days": 0,
            "absent_days": 0,
            "late_days": 0,
            "present_rate": 0.0,
            "by_status": {},
        }
    counts = Counter(r["status"] for r in records)
    present_days = counts.get(PRESENT_STATUS, 0)
    return {
        "total_days": total,
        "present_days": present_days,
        "leave_days": counts.get("請假", 0),
        "sick_days": counts.get("病假", 0),
        "absent_days": counts.get("缺席", 0),
        "late_days": counts.get("遲到", 0),
        "present_rate": present_days / total if total else 0.0,
        "by_status": dict(counts),
    }


def pick_highlight_observations(observations, *, max_count: int = 5) -> list[dict]:
    """選 max_count 筆觀察，優先 is_highlight=True，依 observation_date desc."""
    obs = list(observations)
    obs.sort(
        key=lambda o: (
            bool(getattr(o, "is_highlight", False)),
            getattr(o, "observation_date", None),
        ),
        reverse=True,
    )
    picked = obs[:max_count]
    return [
        {
            "id": o.id,
            "observation_date": (
                o.observation_date.isoformat()
                if getattr(o, "observation_date", None)
                else None
            ),
            "narrative": getattr(o, "narrative", ""),
            "domain": getattr(o, "domain", None),
            "is_highlight": bool(getattr(o, "is_highlight", False)),
        }
        for o in picked
    ]


def measurements_to_series(measurements) -> dict[str, list[tuple]]:
    """轉換為 chart-friendly 結構：{metric: [(date_iso, value), ...]} asc."""
    out: dict[str, list[tuple]] = {
        "height": [],
        "weight": [],
    }
    sorted_m = sorted(measurements, key=lambda m: m.measured_on)
    for m in sorted_m:
        d = m.measured_on.isoformat()
        if m.height_cm is not None:
            out["height"].append((d, float(m.height_cm)))
        if m.weight_kg is not None:
            out["weight"].append((d, float(m.weight_kg)))
    return out
