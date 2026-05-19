import pytest
from utils.calendar_colors import (
    LAYER_COLORS,
    APPRAISAL_MILESTONE_LABELS,
    MEETING_TYPE_LABELS,
    ALL_LAYERS,
)


def test_all_layers_set_has_six():
    assert ALL_LAYERS == {
        "event",
        "holiday",
        "leave",
        "activity",
        "appraisal",
        "meeting",
    }


def test_every_layer_has_default_color():
    for layer in ALL_LAYERS:
        assert layer in LAYER_COLORS, f"{layer} missing default color"
        assert LAYER_COLORS[layer]["default"].startswith("#")
        assert len(LAYER_COLORS[layer]["default"]) == 7


def test_event_layer_has_acknowledge_variant():
    assert "ack" in LAYER_COLORS["event"]


def test_holiday_layer_has_workday_override_variant():
    assert "workday_override" in LAYER_COLORS["holiday"]


def test_leave_layer_has_pending_variant():
    assert "pending" in LAYER_COLORS["leave"]


def test_appraisal_milestone_labels_three_keys():
    assert set(APPRAISAL_MILESTONE_LABELS) == {
        "start_date",
        "end_date",
        "base_score_calc_date",
    }


def test_meeting_type_labels_has_staff_meeting():
    assert MEETING_TYPE_LABELS["staff_meeting"] == "園務會議"
