"""Target: api/insurance.py::GET /insurance/calculate(HTTP 層)。

被攻擊對象:HTTP query string 進到 endpoint 的整條路徑(FastAPI 解析 → auth →
service.calculate → JSON 序列化)。

跟 insurance_target 的差別:
- insurance_target 直接呼叫 calculate(),測 service 自身行為
- 本 target 走 HTTP,測「endpoint 是否正確處理 service 丟出的 ValueError」
  與「response shape 是否合法 JSON(NaN 不可被 JSON serialize)」

不變量(8 條):
- IV1 status_2xx_or_4xx: 永不 5xx(server 內部 exception 漏出視為 bug)
- IV2 valid_json_response: response 永遠是合法 JSON
- IV3 nan_inf_rejected_with_4xx: salary=NaN 應回 422 或 400(已修)
- IV4 success_shape: 200 時 response 含 insured_amount/labor_employee/health_employee
- IV5 finite_premiums: 200 時所有保費欄位都是有限數
- IV6 nonneg_premiums: 200 時所有保費 >= 0
- IV7 missing_salary_4xx: 缺 salary 參數應回 422
- IV8 negative_salary_4xx: 負 salary 應回 422 或 400
"""

from __future__ import annotations

import math

from evals.core.target import Invariant, Target


def _build_client():
    """lazy 建立 TestClient + override auth dep。每次呼叫都建新的(避免 fixture 跨 case 殘留)。"""
    from fastapi.testclient import TestClient

    from main import app
    from utils.auth import get_current_user

    def fake_user():
        return {
            "id": 1,
            "username": "admin",
            "role": "admin",
            "permissions": 0xFFFFFFFFFFFFFFFF,  # 所有 bit
        }

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


_CLIENT = None


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def _runner(case: dict) -> dict:
    """組 query string 打 endpoint。case 內的 None 值不傳。"""
    client = _get_client()
    params = {k: v for k, v in case.items() if v is not None}
    resp = client.get("/api/insurance/calculate", params=params)
    out = {
        "status_code": resp.status_code,
        "headers_ct": resp.headers.get("content-type", ""),
    }
    # 嘗試解析 JSON
    try:
        out["body"] = resp.json()
        out["body_parse_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["body"] = resp.text[:500]
        out["body_parse_ok"] = False
        out["body_parse_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ─────────── invariants ───────────


def _iv_status_2xx_or_4xx(case, outcome):
    if not outcome.get("ok"):
        return None
    sc = outcome["result"]["status_code"]
    if sc >= 500:
        return f"5xx response: {sc}(server error 漏出)"
    return None


def _iv_valid_json_response(case, outcome):
    if not outcome.get("ok"):
        return None
    if not outcome["result"].get("body_parse_ok"):
        return f"response 非合法 JSON: {outcome['result'].get('body_parse_error')}"
    return None


def _iv_nan_inf_rejected_with_4xx(case, outcome):
    """只針對 NaN(string 或 float)。+inf 走 cap-to-max 是 service by-design。"""
    if not outcome.get("ok"):
        return None
    s = case.get("salary")
    is_nan_target = False
    if isinstance(s, float) and math.isnan(s):
        is_nan_target = True
    elif isinstance(s, str) and s.lower() == "nan":
        is_nan_target = True
    if not is_nan_target:
        return None
    sc = outcome["result"]["status_code"]
    if not 400 <= sc < 500:
        return f"salary=NaN 應回 4xx,實得 {sc}"
    return None


def _iv_success_shape(case, outcome):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if res["status_code"] != 200:
        return None
    body = res.get("body")
    if not isinstance(body, dict):
        return f"200 response body 不是 dict: {type(body).__name__}"
    required = {"insured_amount", "labor_employee", "health_employee"}
    missing = required - set(body.keys())
    if missing:
        return f"200 response 缺欄位: {missing}"
    return None


def _iv_finite_premiums(case, outcome):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if res["status_code"] != 200:
        return None
    body = res.get("body")
    if not isinstance(body, dict):
        return None
    for k in (
        "insured_amount",
        "labor_employee",
        "labor_employer",
        "health_employee",
        "health_employer",
        "pension_employer",
        "total_employee",
    ):
        v = body.get(k)
        if isinstance(v, float) and not math.isfinite(v):
            return f"{k}={v} 非有限數(NaN 透過 JSON 漏出)"
    return None


def _iv_nonneg_premiums(case, outcome):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if res["status_code"] != 200:
        return None
    body = res.get("body")
    if not isinstance(body, dict):
        return None
    for k in (
        "labor_employee",
        "labor_employer",
        "health_employee",
        "health_employer",
        "pension_employer",
        "total_employee",
    ):
        v = body.get(k)
        if isinstance(v, (int, float)) and v < 0:
            return f"{k}={v} 為負保費"
    return None


def _iv_missing_salary_4xx(case, outcome):
    if not outcome.get("ok"):
        return None
    if "salary" in case and case["salary"] is not None:
        return None
    sc = outcome["result"]["status_code"]
    if not 400 <= sc < 500:
        return f"missing salary 應回 4xx,實得 {sc}"
    return None


def _iv_negative_salary_4xx(case, outcome):
    if not outcome.get("ok"):
        return None
    s = case.get("salary")
    # 數字型負值
    if not isinstance(s, (int, float)):
        return None
    if isinstance(s, float) and (math.isnan(s) or math.isinf(s)):
        return None
    if s >= 0:
        return None
    sc = outcome["result"]["status_code"]
    if not 400 <= sc < 500:
        return f"salary={s} (負) 應回 4xx,實得 {sc}"
    return None


TARGET = Target(
    name="insurance_endpoint",
    description=(
        "GET /api/insurance/calculate?salary=&dependents= 完整 HTTP 路徑;"
        "TestClient + override get_current_user 為 fake admin"
    ),
    signature={
        "fields": {
            "salary": {
                "type": "float",
                "boundary": [
                    None,
                    0,
                    1,
                    30000,
                    -1,
                    1e9,
                    "NaN",
                    "Infinity",
                    "-Infinity",
                ],
            },
            "dependents": {"type": "int", "boundary": [None, -1, 0, 1, 2, 3, 4, 100]},
        },
        "notes": (
            "FastAPI Query 對 ?salary=NaN 解析為 float('nan');"
            "service 已修補會 raise ValueError → endpoint 應 catch 回 4xx 而非 5xx"
        ),
    },
    invariants=[
        Invariant("IV1_no_5xx", "永不 5xx", _iv_status_2xx_or_4xx),
        Invariant("IV2_valid_json", "response 是合法 JSON", _iv_valid_json_response),
        Invariant(
            "IV3_nan_inf_4xx", "NaN/inf salary 應回 4xx", _iv_nan_inf_rejected_with_4xx
        ),
        Invariant("IV4_success_shape", "200 response 包含必填欄位", _iv_success_shape),
        Invariant("IV5_finite_premiums", "保費欄位皆有限", _iv_finite_premiums),
        Invariant("IV6_nonneg_premiums", "保費欄位皆 >= 0", _iv_nonneg_premiums),
        Invariant("IV7_missing_salary_4xx", "缺 salary 應 4xx", _iv_missing_salary_4xx),
        Invariant(
            "IV8_negative_salary_4xx", "負 salary 應 4xx", _iv_negative_salary_4xx
        ),
    ],
    seed_cases=[
        {"salary": 30000, "dependents": 0},
        {"salary": 45800, "dependents": 2},
    ],
    runner=_runner,
    allowed_exceptions=(),
)
