"""三個 batch-approve 端點補 response_model 的契約測試。

leaves / overtimes / punch_corrections 的 batch-approve 回傳形狀一致
（{"succeeded": list[int], "failed": list[{id, reason}]}），共用
schemas._common.BatchApproveResultOut。

加 response_model 前：FastAPI 對無 response_model 的端點輸出 200 schema == {}（無型別）
→ 前端 codegen 收到 unknown。加 response_model 後：三端點 200 response 皆 $ref
BatchApproveResultOut，前端拿到具名型別。

回傳「形狀」不變的回歸保證由既有大量 batch-approve 測試
（test_punch_correction_batch_approve / test_leaves_batch_* / test_leaves_overtimes_*）
涵蓋；本檔僅鎖「契約已 typed」。
"""

import os
import sys

from fastapi import FastAPI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from api.punch_corrections import router as punch_corrections_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(leaves_router)
    app.include_router(overtimes_router)
    app.include_router(punch_corrections_router)
    return app


def test_three_batch_approve_endpoints_ref_shared_schema():
    app = _build_app()
    spec = app.openapi()
    batch_paths = sorted(p for p in spec["paths"] if p.endswith("batch-approve"))
    # leaves / overtimes / punch-corrections 三條
    assert len(batch_paths) == 3, f"預期 3 條 batch-approve，實得 {batch_paths}"

    for path in batch_paths:
        schema = spec["paths"][path]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert schema != {}, f"{path} 應有具名 typed response schema（非空 {{}}）"
        ref = schema.get("$ref", "")
        assert ref.endswith(
            "BatchApproveResultOut"
        ), f"{path} 200 response 應 $ref BatchApproveResultOut，實得 {schema}"


def test_batch_approve_result_schema_fields_typed():
    """共用 schema 的欄位型別具體（succeeded: int 陣列、failed: 物件陣列），
    確認 IvyBaseModel 不會把欄位退化成 unknown（codegen 友善）。"""
    app = _build_app()
    comps = app.openapi()["components"]["schemas"]
    assert "BatchApproveResultOut" in comps
    assert "BatchApproveFailItem" in comps

    result = comps["BatchApproveResultOut"]["properties"]
    # succeeded: array of integer
    assert result["succeeded"]["type"] == "array"
    assert result["succeeded"]["items"].get("type") == "integer"
    # failed: array referencing BatchApproveFailItem
    assert result["failed"]["type"] == "array"
    assert result["failed"]["items"].get("$ref", "").endswith("BatchApproveFailItem")

    fail = comps["BatchApproveFailItem"]["properties"]
    assert fail["id"].get("type") == "integer"
    assert fail["reason"].get("type") == "string"
