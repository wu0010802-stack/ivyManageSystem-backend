"""TimelineEvent/TimelineOut 應可從 recruitment_timeline 與 recruitment_funnel 兩處 import（後者 re-export）。"""


def test_import_from_timeline_module():
    from schemas.recruitment_timeline import TimelineEvent, TimelineOut

    assert TimelineOut.model_fields["events"] is not None
    assert "source" in TimelineEvent.model_fields


def test_reexport_from_funnel_module():
    from schemas.recruitment_funnel import TimelineEvent as TE
    from schemas.recruitment_timeline import TimelineEvent as TE2

    assert TE is TE2  # 同一個 class（re-export 非複製）
