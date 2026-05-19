"""管理端行事曆 admin_feed 各 layer 顏色與 label 常數。

設計準則：
- 前端 `ivy-frontend/src/constants/calendarLayers.ts` 必須與本檔同步
- 後端固定下發 color，讓前端可純粹按 item.color 渲染、不需重新對照
"""

from typing import Final

ALL_LAYERS: Final[set[str]] = {
    "event",
    "holiday",
    "leave",
    "activity",
    "appraisal",
    "meeting",
}

LAYER_COLORS: Final[dict[str, dict[str, str]]] = {
    "event": {
        "default": "#10b981",
        "ack": "#ef4444",
    },
    "holiday": {
        "default": "#f59e0b",
        "workday_override": "#6366f1",
    },
    "leave": {
        "default": "#0ea5e9",
        "pending": "#94a3b8",
    },
    "activity": {"default": "#ec4899"},
    "appraisal": {"default": "#dc2626"},
    "meeting": {"default": "#8b5cf6"},
}

APPRAISAL_MILESTONE_LABELS: Final[dict[str, str]] = {
    "start_date": "開始",
    "end_date": "結束",
    "base_score_calc_date": "基準分結算",
}

MEETING_TYPE_LABELS: Final[dict[str, str]] = {
    "staff_meeting": "園務會議",
}
