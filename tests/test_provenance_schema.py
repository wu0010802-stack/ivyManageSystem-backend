"""provenance 介面 schema 測試。"""

from decimal import Decimal

from schemas.provenance import DerivedValue, SourceRecord


def test_source_record_minimal():
    sr = SourceRecord(
        date="2025-03-01",
        label="遲到",
        amount=Decimal("-50"),
        module="attendance",
        source_id=123,
    )
    assert sr.module == "attendance"
    assert sr.amount == Decimal("-50")


def test_derived_value_defaults_and_serialize():
    dv = DerivedValue(
        key="attendance_late",
        value=Decimal("-250.00"),
        formula_summary="遲到 5 次 × −50",
        breakdown={"late_count": 5},
        source_records=[
            SourceRecord(
                date="2025-03-01",
                label="遲到",
                amount=Decimal("-50"),
                module="attendance",
                source_id=1,
            )
        ],
        deep_link="/attendance?employee_id=7",
    )
    # override 欄位預留、預設關閉
    assert dv.is_override is False
    assert dv.override_meta is None
    dumped = dv.model_dump()
    assert dumped["key"] == "attendance_late"
    assert len(dumped["source_records"]) == 1


def test_source_record_optional_source_id_defaults_to_none():
    sr = SourceRecord(
        date="2025-03-01", label="x", amount=Decimal("0"), module="attendance"
    )
    assert sr.source_id is None


def test_derived_value_default_collections():
    dv = DerivedValue(key="k", value=Decimal("0"), formula_summary="無紀錄")
    assert dv.breakdown == {}
    assert dv.source_records == []
