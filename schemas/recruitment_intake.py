"""schemas/recruitment_intake.py — 新生名額規劃 in/out。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ReserveSeatIn(BaseModel):
    provisional_grade_id: Optional[int] = Field(
        None, description="暫定年級；null = 釋放保留座位"
    )
    target_school_year: Optional[int] = Field(None, description="目標學年（民國）")
    target_semester: Optional[int] = Field(None, description="目標學期，省略預設 1")


class ReserveSeatOut(BaseModel):
    visit_id: int
    provisional_grade_id: Optional[int]
    provisional_grade_name: Optional[str]
    target_school_year: Optional[int]
    target_semester: Optional[int]


class IntakePlanRow(BaseModel):
    grade_id: int
    grade_name: str
    target_seats: int
    reserved_count: int
    enrolled_count: int
    remaining: int
    over_capacity: bool


class IntakePlanOut(BaseModel):
    school_year: int
    semester: int
    rows: list[IntakePlanRow]


class IntakeTargetItem(BaseModel):
    grade_id: int
    target_seats: int = Field(ge=0)


class IntakeTargetsIn(BaseModel):
    school_year: int
    semester: int = 1
    targets: list[IntakeTargetItem]


class IntakeTargetsOut(BaseModel):
    school_year: int
    semester: int
    targets: list[IntakeTargetItem]
