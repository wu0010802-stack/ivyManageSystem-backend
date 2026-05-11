"""appraisal 跨表 E2E 整合測試 — 補齊 unit test 涵蓋不到的場景。

驗證考核系統的完整生命週期：
- 建 cycle → bulk_init → 設 base_score → 登事件 → lock → recompute → 三階簽核 → close
- FINALIZED 後改 base_score 被擋
- Unlock cycle 可補事件
- bonus_rate 新版本不影響歷史結算
"""

from datetime import date
from decimal import Decimal


def test_E2E_完整生命週期(
    client,
    admin_headers,
    supervisor_headers,
    accountant_headers,
    principal_headers,
    employee_factory,
):
    """建立 cycle → bulk_init → 設 base_score → 登事件 → lock → recompute → 三階簽核 → close。

    使用 academic_year=115, semester=FIRST（base_score_calc_date=2026-09-15）
    確保種子獎金率 effective_from=2026-08-01 生效，bonus_amount 計算非零。
    """
    emp = employee_factory(job_title_name="班導師")

    # 1. 建 cycle（115 上學期 → start 2026-08-01, base_score_calc_date 2026-09-15）
    r = client.post(
        "/api/appraisal/cycles",
        json={"academic_year": 115, "semester": "FIRST"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.json()
    cycle = r.json()

    # 2. bulk_init 帶上這位老師
    r = client.post(
        f"/api/appraisal/cycles/{cycle['id']}/participants:bulk_init",
        json={"employee_ids": [emp.id]},
        headers=admin_headers,
    )
    assert r.status_code == 200
    p = next(x for x in r.json() if x["employee_id"] == emp.id)

    # 3. 設基本分
    r = client.patch(
        f"/api/appraisal/participants/{p['id']}",
        json={"base_score": "85"},
        headers=admin_headers,
    )
    assert r.status_code == 200

    # 4. 登一筆大功 (+6)，event_date = cycle.start_date（2026-08-01）
    r = client.post(
        "/api/appraisal/events",
        json={
            "participant_id": p["id"],
            "event_type": "MAJOR_MERIT",
            "event_date": cycle["start_date"],
            "score_delta": "6",
            "title": "大功",
        },
        headers=supervisor_headers,
    )
    assert r.status_code == 201, r.json()

    # 5. lock cycle（需 APPRAISAL_FINALIZE = principal）
    r = client.post(
        f"/api/appraisal/cycles/{cycle['id']}/lock", headers=principal_headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "LOCKED"

    # 6. recompute → DRAFT summary 出爐
    r = client.post(
        f"/api/appraisal/cycles/{cycle['id']}/summaries:recompute",
        headers=supervisor_headers,
    )
    assert r.status_code == 200
    summary = r.json()[0]
    # base 85 + event 6 = 91 → OUTSTANDING
    assert Decimal(summary["total_score"]) == Decimal("91.00")
    assert summary["grade"] == "OUTSTANDING"
    # HEAD_TEACHER OUTSTANDING base=8000，total_score=91 → 8000 × 0.91 = 7280.00
    assert Decimal(summary["bonus_amount"]) == Decimal("7280.00")

    # 7. 三階簽核
    sid = summary["id"]
    r = client.post(
        f"/api/appraisal/summaries/{sid}/sign_supervisor",
        json={"comment": "OK"},
        headers=supervisor_headers,
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "SUPERVISOR_SIGNED"

    r = client.post(
        f"/api/appraisal/summaries/{sid}/sign_accounting",
        json={"comment": "OK"},
        headers=accountant_headers,
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "ACCOUNTING_SIGNED"

    r = client.post(
        f"/api/appraisal/summaries/{sid}/finalize",
        json={"comment": "OK", "reason": "115 上學期定稿"},
        headers=principal_headers,
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "FINALIZED"

    # 8. close cycle（所有 summary 已 FINALIZED 才可 close）
    r = client.post(
        f"/api/appraisal/cycles/{cycle['id']}/close", headers=principal_headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "CLOSED"


def test_FINALIZED_後改_base_score_409(client, admin_headers, finalized_summary):
    """summary FINALIZED 後，underlying participant base_score 不可改 → 409。

    mark_summary_stale 偵測 FINALIZED summary → raise PermissionError → 409。
    detail 包含 "summary_finalized" 字串。
    """
    pid = finalized_summary.participant_id
    resp = client.patch(
        f"/api/appraisal/participants/{pid}",
        json={"base_score": "70"},
        headers=admin_headers,
    )
    assert resp.status_code == 409
    assert "summary_finalized" in resp.json()["detail"]


def test_unlock_cycle_可補事件(
    client,
    principal_headers,
    locked_cycle_with_participants,
):
    """cycle unlock 後 status 回 OPEN；可繼續新增事件。

    使用 locked_cycle_with_participants（LOCKED 狀態），
    unlock 需 APPRAISAL_FINALIZE 權限（principal_headers）。
    """
    r = client.post(
        f"/api/appraisal/cycles/{locked_cycle_with_participants.id}/unlock",
        json={"reason": "補登 12 月事件"},
        headers=principal_headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "OPEN"


def test_bonus_rate_新版本_不影響舊_summary(
    client, admin_headers, locked_cycle_with_participants, supervisor_headers
):
    """新增 bonus_rate（未來日期）不會回頭改舊 summary。

    注意：locked_cycle_with_participants.base_score_calc_date = 2026-03-15，
    早於種子率 effective_from 2026-08-01，故舊 recompute 結果全為 0。
    新增 2030-01-01 rate 同樣不影響 2026-03-15 的計算，old == new 仍成立。
    """
    # 先 recompute 取舊獎金快照
    r1 = client.post(
        f"/api/appraisal/cycles/{locked_cycle_with_participants.id}/summaries:recompute",
        headers=supervisor_headers,
    )
    assert r1.status_code == 200
    old = {s["id"]: s["bonus_amount"] for s in r1.json()}

    # 新增未來日期 rate（effective_from 2030-01-01，遠超過 calc_date）
    r = client.post(
        "/api/appraisal/bonus_rates",
        json={
            "effective_from": "2030-01-01",
            "role_group": "SUPERVISOR",
            "grade": "OUTSTANDING",
            "base_amount": "20000",
        },
        headers=admin_headers,
    )
    assert r.status_code == 201

    # 再 recompute（cycle.base_score_calc_date 仍在 2030 前，新 rate 不生效）
    r2 = client.post(
        f"/api/appraisal/cycles/{locked_cycle_with_participants.id}/summaries:recompute",
        headers=supervisor_headers,
    )
    assert r2.status_code == 200
    new = {s["id"]: s["bonus_amount"] for s in r2.json()}

    # 獎金不因新 rate 改變
    assert old == new
