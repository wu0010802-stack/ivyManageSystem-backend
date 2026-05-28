"""Recruitment records router (api/recruitment/records.py) Out schemas。

Phase 3.5 範圍（本檔）：
- RecruitmentRecordOut (對應 shared._to_dict shape)
- RecruitmentRecordListOut (GET /records 分頁回傳)
- RecruitmentRecordImportResultOut (POST /import inserted/skipped 計數)
- RecruitmentRecordConvertOut (POST /records/{id}/convert deprecated 轉化結果)

5 endpoint wired:
- GET    /records                       → RecruitmentRecordListOut
- POST   /records                       → RecruitmentRecordOut
- PUT    /records/{record_id}           → RecruitmentRecordOut
- POST   /import                        → RecruitmentRecordImportResultOut
- POST   /records/{record_id}/convert   → RecruitmentRecordConvertOut

Out of scope (defer)：
- DELETE /records/{record_id} (status 204 no body — 與 periods.py:delete_period 同 pattern)

PII：家長電話 / 學生姓名 / 地址 / 行政區 / 介紹者 / 收預繳人員 等皆為招生敏感資料，
admin/RECRUITMENT_READ 端 dashboard 必看，全部以 # pii-allow 標註。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class RecruitmentRecordOut(IvyBaseModel):
    """招生訪視紀錄單筆 — 對應 api/recruitment/shared._to_dict shape。"""

    id: int
    month: Optional[str] = None
    seq_no: Optional[str] = None
    visit_date: Optional[str] = None
    child_name: Optional[str] = (
        None  # pii-allow: 招生幼生姓名（admin RECRUITMENT_READ 必看）
    )
    birthday: Optional[str] = None  # pii-allow: 招生幼生生日（ISO 字串）
    grade: Optional[str] = None
    phone: Optional[str] = None  # pii-allow: 家長聯絡電話（招生追蹤必看）
    address: Optional[str] = None  # pii-allow: 家長居住地址（行政區/熱點分析）
    district: Optional[str] = None  # pii-allow: 行政區（與 address 同源）
    source: Optional[str] = None
    referrer: Optional[str] = None  # pii-allow: 介紹者姓名（招生關係追蹤）
    deposit_collector: Optional[str] = None  # pii-allow: 收預繳人員姓名（內部稽核）
    has_deposit: Optional[bool] = None
    notes: Optional[str] = None  # pii-allow: 訪視備註（可能含家長情境描述）
    parent_response: Optional[str] = None  # pii-allow: 電訪後家長回應（含家庭情況）
    no_deposit_reason: Optional[str] = None
    no_deposit_reason_detail: Optional[str] = None  # pii-allow: 未預繳細節描述
    enrolled: Optional[bool] = None
    transfer_term: Optional[bool] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RecruitmentRecordListOut(IvyBaseModel):
    """GET /recruitment/records — 分頁回傳。"""

    total: int
    page: int
    page_size: int
    records: list[RecruitmentRecordOut]


class RecruitmentRecordImportResultOut(IvyBaseModel):
    """POST /recruitment/import — Excel 批次匯入結果。

    與 _common.ImportResultOut 不同：本 endpoint 僅回計數，不回失敗明細
    （match shared._to_dict 既有行為，避免破壞既有前端契約）。
    """

    inserted: int
    skipped: int


class RecruitmentRecordConvertOut(IvyBaseModel):
    """POST /recruitment/records/{record_id}/convert — 招生訪視 → 正式學生轉化結果。

    本 endpoint 已 deprecated（改用 POST /recruitment/funnel/visits/{visit_id}/transition），
    保留 schema 維持向後相容。
    """

    message: str
    student_id: int
    recruitment_visit_id: int
    primary_guardian_id: Optional[int] = (
        None  # pii-allow: 主要監護人 ID（非 PII 本身，僅內部 FK；admin 端 reference）
    )
    change_log_id: Optional[int] = None
