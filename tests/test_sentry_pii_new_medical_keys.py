"""PII denylist 補齊醫療/監護特種個資 key 的 lexical gap（SEC-003 / 資安掃描 2026-06-15 P2）。

_PII_KEY_SUBSTRINGS 有 'allergy' 但 'allergy' 並非 'allergen' / 'allergies' 的子字串
（index 6 起 y vs i/e），故 _key_is_pii('allergen') 回 False；'reaction_symptom' /
'first_aid_note'（StudentAllergy 欄位）與 'custody_note'（監護權備註）同樣漏網。
任何未來 log / audit / Sentry-extra 帶這些 key 即單側洩漏特種個資。

修法：兩端 denylist（utils/sentry_init.py 與 ../ivy-frontend/src/utils/sentry.ts，
集合須一致，見 test_pii_denylist_parity）補上這些 key。
"""

import pytest

from utils.sentry_init import _key_is_pii

NEW_KEYS = [
    "allergen",
    "allergies",
    "reaction_symptom",
    "first_aid_note",
    "custody_note",
]


@pytest.mark.parametrize("key", NEW_KEYS)
def test_new_medical_pii_keys_are_scrubbed(key):
    assert _key_is_pii(key), f"{key} 應被視為 PII key 而遮罩"


def test_extended_field_names_with_new_keys_are_scrubbed():
    # substring 匹配也涵蓋延伸欄位名
    assert _key_is_pii("student_allergen")
    assert _key_is_pii("primary_custody_note")
    assert _key_is_pii("child_reaction_symptom")
