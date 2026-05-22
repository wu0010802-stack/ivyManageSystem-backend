"""Pydantic schemas for /academic-terms."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class AcademicTermIn(BaseModel):
    school_year: int = Field(..., ge=100, le=200, description="民國學年")
    semester: int = Field(..., ge=1, le=2)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_dates(self) -> "AcademicTermIn":
        if self.end_date <= self.start_date:
            raise ValueError("end_date 必須晚於 start_date")
        return self


class AcademicTermOut(BaseModel):
    id: int
    school_year: int
    semester: int
    start_date: date
    end_date: date
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True
