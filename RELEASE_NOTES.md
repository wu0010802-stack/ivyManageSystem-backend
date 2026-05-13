# Release Notes

## 2026-05-13 leave↔OT 跨類抵扣（feature flag, v1）

新增 `ENABLE_LEAVE_OT_OFFSET` 環境變數（預設 `false`）。啟用後，approve leave 時若同員工同日有已核准的 OT，會於 ApprovalLog 留下 `offset_by_leave_id` metadata 紀錄；同時 audit_changes 帶 `cross_offset_ot_id`。

### 啟用方式
```bash
ENABLE_LEAVE_OT_OFFSET=true  # 接受 true/1/yes（不分大小寫）
```

### 已知限制（v1）
- **不影響 salary engine**：`OvertimeRecord` 目前沒有 `paid` / `offset_by_leave_id` 欄位，本版本**僅在 ApprovalLog 留 metadata 軌跡**，salary engine 仍會把該 OT 加班費計入該月薪資。要真正抵扣加班費需後續：
  1. 為 `OvertimeRecord` 加 `offset_by_leave_id` FK 欄位（並 Alembic migration）
  2. 修改 `services/salary/engine.py` 在彙總 OT 時跳過已 offset 的記錄
  3. 評估再算月份的 finalize 守衛與 stale 標記策略
- **跨日 leave 僅偵測 `start_date` 那一天**：多日 leave 的其他日期 OT 不會被偵測；後續版本可擴展為對 `start_date ~ end_date` 區間每日各偵測一次。
- **單向觸發**：僅在 approve leave 流程偵測，approve OT 時不反向偵測 leave。雙向觸發會引入 race 條件且首版風險過高，暫不實作。
- **補休假單已排除**：`leave.source_overtime_id` 非空時不再做 offset（避免雙重抵扣已綁定的 OT）。
- **`use_comp_leave=True` 的 OT 已排除**：已選擇以補休代替加班費的 OT 本身就不會發放現金，不需 offset。

### 金流影響
- **v1 純記錄**：啟用此 flag **不會**改變任何員工的應發金額；只在 ApprovalLog 多一筆 metadata 記錄。
- 後續若接通 salary engine（OT 被 offset 後跳過計薪），啟用 flag 將降低加班費總額，屆時必須：
  1. dev DB 跑 115.04 對齊驗證確認金額無誤
  2. 通知財務 / 業主啟用時點
  3. 評估歷史月份是否要回溯處理（建議只對未封存月份生效）

### 測試
- `tests/test_cross_type_offset.py`（7 個案例，含 flag on/off、補休短路、未核准 OT、決定性排序）

### Audit 軌跡
- Leave ApprovalLog：原有 `[META]` 不變
- Overtime ApprovalLog（新）：`doc_type="overtime"`, `action="update"`, `comment="leave 跨類抵扣（auto, v1 metadata-only）"`, `metadata={"offset_by_leave_id": <leave_id>, "offset_date": "YYYY-MM-DD"}`
- AuditLog changes：新增 `cross_offset_ot_id` 欄位（無偵測時為 `null`）
