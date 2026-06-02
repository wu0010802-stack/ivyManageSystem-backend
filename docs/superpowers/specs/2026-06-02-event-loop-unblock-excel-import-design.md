# 2026-06-02 後端 Excel 匯入端點解 event loop 阻塞 — Design

## 背景

2026-06-02 跨前後端效能審查（靜態結構分析 + 真 build bundle 分析）發現：後端架構為「sync-first」，710 個 route handler 中僅 ~37 個 `async def`，絕大多數同步工作由 FastAPI 自動丟 threadpool，**不卡 event loop**（合理且安全的選擇）。真正的系統性問題落在少數 `async def` 端點：它們在 async 路徑上混入**同步阻塞 IO**，沒有 `run_in_executor` 卸載 → 阻塞唯一的 event loop。

最嚴重者為三個 Excel 批次匯入端點：

| 端點 | 位置 |
|------|------|
| `import_leaves` | `api/leaves.py:2283` |
| `import_overtimes` | `api/overtimes.py:1777` |
| `import_shifts` | `api/shifts.py:779` |

三者皆 `async def`，正確 `await read_upload_with_size_check(file)`（async IO），但隨後在 **event loop 上**直接跑：

1. `parse_excel(BytesIO(content), ...)` — openpyxl 解析整份檔案（CPU-bound，數百 ms～數秒）
2. 同步 DB 迴圈 — `get_session()` → `build_employee_lookup` → 逐列 insert/commit

匯入一份數百列的 Excel 期間，**整台 server 所有並發請求停擺**（含 WebSocket 推播、健康檢查、以及其他端點賴以卸載工作的 executor）。

同 repo `api/dismissal_calls.py:272 create_dismissal_call` 已示範正確 pattern：DB 工作用 `loop.run_in_executor` 卸載，只把 WebSocket `await` 留在 async 層。

## 已校正的審查前提

審查另指 `GET /audit-logs/export` 無上限載入整張稽核表。**實讀代碼修正**：該端點已有 `EXPORT_MAX_ROWS = 10000` 硬上限（`api/audit.py:184-189`，超過 raise 400），且為 sync `def` → threadpool（不卡 event loop），匯出 CSV（非 xlsx）。風險被審查高估。`yield_per(500)` 串流仍能降峰值記憶體，但屬邊際優化，**列為可選 follow-up，不在本 spec 主範圍**。

## 目標

消除三個 Excel 匯入端點對 event loop 的阻塞，使匯入期間其他並發請求不受影響。**對外行為完全不變**（同樣的解析、驗證、錯誤訊息、HTTP status、回傳格式）。

## 非目標

- 不改匯入的業務邏輯／驗證規則／錯誤訊息格式（純結構性卸載）。
- 不動 audit export（已有 cap，可選 follow-up）。
- 不引入背景任務佇列（Celery 等）— 這些是同步請求內可接受的工作，只需卸載出 event loop，不需轉成 async job。

## 方案

每個端點抽出一個 `_db_import_*_sync(content: bytes, user_id) -> result` 純同步函式，涵蓋：

- `parse_excel(...)`
- file-level error 檢查（`INVALID_FILE` / `EMPTY_FILE` / `MISSING_COLUMN` → `raise HTTPException(400)`）
- session 開啟 + 逐列處理 + commit + `session.close()`
- 回傳 result dict

`async def` 端點層只保留：

```python
content = await read_upload_with_size_check(file)   # async IO，留 async 層
validate_file_signature(content, ".xlsx")            # 快速 sync
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, _db_import_leaves_sync, content, user_id)
return result
```

**關鍵細節：**

- file-level error 在 `_db_import_*_sync` 內 `raise HTTPException(400)`；`run_in_executor` 會把例外 re-raise 在 `await` 點，FastAPI exception handler 照常捕獲 → 行為一致。
- session 在 sync 函式內自開自關（已是現狀寫法，搬進 executor thread 天然正確 — 每個 thread 自己的 session）。
- `current_user` 只取出 `user_id`（plain value）傳入 executor，不要把整個 request-scoped 物件帶進 thread。
- 三端點結構相近但非完全相同（plan 階段逐一確認 overtimes / shifts 的 file-level check 與回傳格式），**各自抽各自的 sync 函式，不強行共用**（避免引入跨端點耦合，違反 surgical 原則）。

## 測試

1. **Characterization test 先行**：若現有 pytest 未覆蓋三端點的「成功匯入 / file-level error / row-level error」三條路徑，先補能重現現有行為的測試（成功筆數、失敗筆數、錯誤訊息格式、HTTP status）。
2. 重構後同一批測試必須全綠 — 這是「行為不變」的證明。
3. 不另寫「不阻塞 event loop」的單元測試（難以可靠單元測試，且 pattern 已由 `dismissal_calls` 驗證）；以 code review 確認 `run_in_executor` 包裹正確。

## 風險

低-中。

- 確保 error path 的 `HTTPException` 在 executor 內 raise 後正確 propagate（`run_in_executor` 行為保證）。
- 確保 session 生命週期在 executor thread 內完整（自開自關，現狀已是）。
- 三端點逐一驗證 `response_model` 不變。

## 範圍外 follow-up

- audit export `yield_per(500)` 串流降峰值記憶體（可選）。
- 其餘 async 同步阻塞輕量項（`students.py` delete/graduate 的同步 DB 段、`uptime_webhook` 的同步 `requests.post`）— 低頻、低影響，列入觀察，非本 spec 範圍。
