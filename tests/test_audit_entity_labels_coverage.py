"""Sanity: ENTITY_LABELS coverage for entity_types used by audit middleware + helpers."""

from utils.audit import ENTITY_LABELS, ENTITY_PATTERNS


def test_entity_labels_cover_all_patterns():
    """每個 ENTITY_PATTERNS 的 entity_type 都要在 ENTITY_LABELS 有中文 label。
    否則軟刪/真刪 summary 會回退顯示英文 key。"""
    # ENTITY_PATTERNS structure: list of (regex, entity_type) tuples
    pattern_keys = {entity_type for _, entity_type in ENTITY_PATTERNS}
    label_keys = set(ENTITY_LABELS.keys())
    missing = pattern_keys - label_keys
    assert (
        not missing
    ), f"ENTITY_LABELS missing entries for ENTITY_PATTERNS entity_types: {sorted(missing)}"


def test_soft_delete_marker_entity_types_have_labels():
    """軟刪 helper 慣用的 entity_type 都要有中文 label，否則 summary 回退英文。"""
    used_types = {
        "attachment",
        "guardian",
        "contact_book_entry",
        "employee",
        "user",
        "student",
    }
    for et in used_types:
        assert et in ENTITY_LABELS, f"{et} 無中文 label，軟刪 summary 會顯示英文"
