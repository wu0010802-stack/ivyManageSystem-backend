"""seedgen IEP jsonb 欄位 shape 須對齊 API 契約（2026-06-15 運作探測 P2-5）。

Bug：scripts/seedgen/modules/m11_special_ed 把 short_term_goals / iep_team_members
  寫成 list[str]、meeting_dates 寫成 list，違反契約（short_term_goals /
  iep_team_members = List[dict]、meeting_dates = dict）→ GET/匯出 IEP 端點對 seed
  資料全 500（list 端點 response_model 嚴格驗證失敗；PDF .get() AttributeError）。
  影響侷限 seeded dev/QA（prod 一律經前端表單送 dict）。
"""

from datetime import date

from api.gov_moe.iep import IepBase
from scripts.seedgen.modules.m11_special_ed import _iep_jsonb_fields


def test_iep_jsonb_fields_conform_to_contract():
    fields = _iep_jsonb_fields(date(2025, 9, 15), semester=1)
    # 以 API 契約 schema 直接驗證：list[str] 會 ValidationError
    model = IepBase(student_id=1, school_year=2025, semester=1, **fields)
    assert isinstance(model.short_term_goals, list)
    assert model.short_term_goals and all(
        isinstance(g, dict) for g in model.short_term_goals
    )
    assert model.iep_team_members and all(
        isinstance(m, dict) for m in model.iep_team_members
    )
    assert isinstance(model.meeting_dates, dict)
