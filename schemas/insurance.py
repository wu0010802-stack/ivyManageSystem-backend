"""Insurance router (api/insurance.py) 對應 Out schemas — Phase 3.5。

涵蓋 4 個 grandfather endpoint（全 admin 後台，無公開）：

- GET    /insurance/calculate               → InsuranceCalculationOut
- GET    /insurance/brackets                → InsuranceBracketListOut
- PUT    /insurance/brackets                → InsuranceBracketUpsertResultOut
- DELETE /insurance/brackets/{bracket_id}   → InsuranceBracketDeleteResultOut

PII 註解：
- 級距表 (``InsuranceBracketItemOut``) 的 ``amount`` / ``labor_*`` / ``health_*``
  / ``pension`` 為政府公告之分級表資料，**不是個別員工的投保金額**；但 PII denylist
  substring 命中（``health`` / ``insured``），故標 ``# pii-allow:`` 並註明來源。
- 計算端 ``InsuranceCalculationOut`` 回傳對應某 query salary 的試算結果，仍非
  特定員工綁定（行政人員自行輸入 salary 試算用），但 ``insured_amount`` /
  ``health_*`` 同樣 substring 命中，標 ``# pii-allow:``。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ============ GET /insurance/calculate ============


class InsuranceCalculationOut(IvyBaseModel):
    """GET /insurance/calculate 試算回傳。

    對應 service.calculate() 結果（InsuranceCalculationResult），純試算不寫 DB；
    呼叫端為行政自行輸入 salary + dependents 預估保費。
    """

    insured_amount: (
        int  # pii-allow: 試算對應之投保級距金額（政府公告分級表，非特定員工綁定）
    )
    labor_employee: int  # pii-allow: 勞保員工負擔（政府公告金額試算）
    labor_employer: int  # pii-allow: 勞保雇主負擔（政府公告金額試算）
    health_employee: int  # pii-allow: 健保員工負擔（政府公告金額試算）
    health_employer: int  # pii-allow: 健保雇主負擔（政府公告金額試算）
    pension_employer: int  # pii-allow: 勞退提撥（政府公告金額試算）
    total_employee: int  # pii-allow: 員工負擔合計（政府公告金額試算）
    total_employer: int  # pii-allow: 雇主負擔合計（政府公告金額試算）


# ============ GET /insurance/brackets ============


class InsuranceBracketItemOut(IvyBaseModel):
    """單筆勞健保級距（政府公告分級表）。

    來源：InsuranceBracket ORM。非個別員工資料，係政府每年公告之投保金額分級表。
    """

    id: int
    amount: int  # pii-allow: 政府公告投保金額分級（非特定員工投保薪資）
    labor_employee: int  # pii-allow: 政府公告勞保員工負擔金額
    labor_employer: int  # pii-allow: 政府公告勞保雇主負擔金額
    health_employee: int  # pii-allow: 政府公告健保員工負擔金額
    health_employer: int  # pii-allow: 政府公告健保雇主負擔金額
    pension: int  # pii-allow: 政府公告勞退提撥金額


class InsuranceBracketListOut(IvyBaseModel):
    """GET /insurance/brackets 回傳。

    `requested_year` 為查詢的目標年；當該年無資料時 fallback 至 `effective_year`
    （≤ requested_year 中最新者）；皆無資料時 `effective_year` 為 None。
    """

    requested_year: int
    effective_year: Optional[int] = None
    brackets: list[InsuranceBracketItemOut]


# ============ PUT /insurance/brackets ============


class InsuranceBracketUpsertResultOut(IvyBaseModel):
    """PUT /insurance/brackets 整張表 upsert 回傳。"""

    message: str
    effective_year: int
    upserted: int
    replaced_existing: bool
    stale_marked: int


# ============ DELETE /insurance/brackets/{bracket_id} ============


class InsuranceBracketDeleteResultOut(IvyBaseModel):
    """DELETE /insurance/brackets/{bracket_id} 回傳。"""

    message: str
    effective_year: int
    stale_marked: int
