"""S3：PublicUpdatePayload.remark 補 max_length=500（對齊 register 版）。

家長公開報名「修改」payload 的 remark 過去為裸 `str = ""`，無長度上限，
可塞任意大字串進 DB；register 版（PublicRegistrationPayload.remark）已有
max_length=500，兩者應一致。
"""

import pytest
from pydantic import ValidationError

from schemas.activity_public import PublicUpdatePayload


def _base_payload(**overrides):
    payload = {
        "id": 1,
        "name": "王小明",
        "birthday": "2020-01-01",
        "class": "大班",
        "parent_phone": "0912345678",
        "courses": [],
    }
    payload.update(overrides)
    return payload


def test_remark_at_500_chars_accepted():
    p = PublicUpdatePayload(**_base_payload(remark="字" * 500))
    assert len(p.remark) == 500


def test_remark_over_500_chars_rejected():
    with pytest.raises(ValidationError):
        PublicUpdatePayload(**_base_payload(remark="字" * 501))


def test_remark_default_empty():
    p = PublicUpdatePayload(**_base_payload())
    assert p.remark == ""
