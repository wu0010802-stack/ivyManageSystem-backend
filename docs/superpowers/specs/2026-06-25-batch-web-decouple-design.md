# Batch/Web 解耦設計（LONG，成長觸發）

**狀態**：設計（成長觸發；當前單 worker 負載用不到，不實作）
**背景**：設計審查 2026-06-25 主題（並發/背景工作）。**觸發條件**：當批次工作
（finance reconciliation / salary snapshot / enrollment roster PDF 等）週期性飽和 20 條
DB 連線池、餓死 web 請求時才做。當前 prod（員工<50、家長<500、單 worker）尚無此壓力。

## 問題

14+ 排程器目前以 **in-process thread**（`asyncio.create_task` + `asyncio.to_thread`）
跑在 web app process（`main.py` `app_lifespan`），與 web serving **同 process 共命運**：
- 失敗隔離缺：某排程器 OOM / 卡死可拖累 web。
- 資源競爭：排程器大查詢與 web 請求共搶同一個 20 連線 pool（pool_timeout 後 web 500）。
- 無法獨立擴展/部署：要加 web 容量就連帶把排程器複製（advisory lock 雖防重複執行，
  但每個 web replica 都載入排程器程式碼、佔 threadpool token）。

## 既有基礎（已支援解耦）

- 每個排程器都有 `scheduler_enabled()`（env flag）+ `utils/advisory_lock.try_scheduler_lock`
  ——**advisory lock 天生支援多 process**：即使多個 process 都啟用同一排程器，只有一個
  搶到 Postgres advisory lock 實際執行。故「web process 不跑排程、獨立 scheduler process
  跑排程」零去重風險。
- `scheduler_observability` 的 heartbeat / watermark 持久化在 DB，跨 process 可見
  （`/health/schedulers` 不需排程器與 web 同 process）。

## 方案

1. 新增 entrypoint `schedulers/__main__.py`（`python -m schedulers`）：只起 asyncio loop
   + 呼叫現有的排程器啟動邏輯（從 `app_lifespan` 抽出成共用 `start_all_schedulers(stop_event)`），
   **不掛 FastAPI app、不收 HTTP**。自帶獨立 DB pool（與 web pool 隔離）。
2. 新增 env flag `RUN_SCHEDULERS_IN_WEB`（預設 `1`＝當前單 process 行為，零改變）。
   - 單 process 部署：`RUN_SCHEDULERS_IN_WEB=1`，web lifespan 照舊起排程器。
   - 解耦部署：web 設 `RUN_SCHEDULERS_IN_WEB=0`（lifespan 跳過排程器啟動）；另起一個
     `python -m schedulers` service（Zeabur 第二個 service / 同 image 不同 start command）。
3. `app_lifespan` 的排程器啟動區塊改為 `if settings.scheduler.run_in_web: start_all_schedulers(...)`。

## 實作要點

- 抽 `start_all_schedulers(stop_event)` / `stop_all_schedulers()` 成 `services/scheduler_runtime.py`
  （把 `app_lifespan` 內 ~15 個 `create_task(...scheduler...)` 收斂成一處），web 與
  `schedulers/__main__` 共用。
- scheduler process 也要起 notification dispatch hooks（若排程器會發通知）+ broadcast backend
  （WS 廣播；解耦後須 `BROADCAST_BACKEND=redis` 才能跨 process 推 WS——與 LONG-1 scale-out
  gate 連動：解耦＝多 process＝`DEPLOYMENT_MODE=multi`，gate 會強制 redis backend）。
- 優雅關閉：SIGTERM → set stop_event → 等 in-flight iteration 收尾。

## 驗證

- 單 process（`RUN_SCHEDULERS_IN_WEB=1`）行為與現況完全一致（回歸）。
- 解耦：web（flag=0）不跑排程、scheduler process 跑；advisory lock 確保同一排程器全域
  只一個實例執行；`/health/schedulers` 仍看得到 heartbeat。
- 同時跑兩個 scheduler process → advisory lock 讓只一個執行（驗去重）。

## 為何不現在做

過早優化：當前單 worker 無 pool 飽和證據；解耦引入「跑 2 個 process」的 ops 複雜度與
「排程器沒起＝背景工作靜默停」的新失敗模式。等出現 pool_timeout 500 與批次/web 互搶的
實測訊號（可由 `/health/db_pool` 與慢請求告警觀察）再觸發。
