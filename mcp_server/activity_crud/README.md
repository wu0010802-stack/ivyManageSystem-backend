# ivy-activity-crud MCP Server

暴露 ivy-backend 的「課後才藝課程 / 用品」CRUD 給 Claude Code 等 MCP client，
讓管理員透過自然語言新增、修改、查詢課程與用品。

---

## 包含的 12 個 tool

### 課程（8）
- `list_courses(school_year?, semester?, skip?, limit?)`
- `get_course(course_id)`
- `create_course(name, price, capacity, ...)`
- `copy_courses_from_previous(source_school_year, source_semester, target_school_year, target_semester)`
- `update_course(course_id, ...partial fields)`
- `delete_course(course_id)` ← 軟刪
- `list_course_waitlist(course_id)`
- `list_course_enrolled(course_id)`

### 用品（4）
- `list_supplies(school_year?, semester?, skip?, limit?)`
- `create_supply(name, price, school_year?, semester?)`
- `update_supply(supply_id, name?, price?)`
- `delete_supply(supply_id)` ← 軟刪

---

## 啟用步驟

### 1. 準備一個 ivy-backend 員工帳號

建議在系統內專開一個 `mcp-bot` 員工，授予 `ACTIVITY_READ + ACTIVITY_WRITE`
（高單價課程 / 用品需要時再加 `ACTIVITY_APPROVE`），方便 audit 識別來源。

dev 環境也可直接用既有 admin 帳號。

### 2. 把帳密放進 shell env

在 `~/.zshrc`（或你的 shell 設定）加：

```bash
export IVY_MCP_USERNAME="mcp-bot"
export IVY_MCP_PASSWORD="..."
```

`source ~/.zshrc` 之後重啟 Claude Code 才能讀到。

### 3. 啟動後端

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh --backend-only
```

確認 `http://localhost:8088/docs` 開得起來。

### 4. Claude Code 認到 MCP server

workspace 的 `.mcp.json` 已加入 `ivy-activity-crud` entry，下次啟動 Claude Code
時會自動載入。確認方式：開新 session 後問「列出可用的 MCP tool」，應看到
`list_courses` 等 12 個 tool。

---

## 手動整合驗證

```text
你：列出本學期所有課後才藝課程
Claude：呼叫 list_courses(...) → 印出 N 個課程
你：新增「兒童芭蕾」課程，週三 16:00~17:00，每堂 1500 元，名額 12 人
Claude：呼叫 create_course(name="兒童芭蕾", price=1500, capacity=12,
        meeting_weekday=2, meeting_start_time="16:00", meeting_end_time="17:00")
你：把它價格改成 1800
Claude：呼叫 update_course(course_id=..., price=1800)
```

每筆呼叫都會在 ivy-backend audit log 留下 `mcp-bot` 操作軌跡。

---

## 常見錯誤

| 訊息 | 對策 |
|---|---|
| `IVY_MCP_USERNAME / IVY_MCP_PASSWORD 未設定` | 確認 env 在啟動 Claude Code 的 shell 內有匯入 |
| `MCP 帳號登入失敗：帳號或密碼錯誤` | 檢查帳密；或目標帳號被停用 |
| `無法連線 ivy-backend (http://localhost:8088)` | 後端沒跑，`./start.sh --backend-only` |
| `課程「X」已存在` | 同學期已有同名課程，後端拒絕 |
| `權限不足：缺少 ACTIVITY_WRITE` | 該帳號 permissions 沒涵蓋寫入，請管理員加權 |
| `單價超過 ... 需 ACTIVITY_APPROVE` | 高單價需簽核權限，請管理員加 ACTIVITY_APPROVE |

---

## 開發與測試

```bash
# 後端 venv 內跑單元測試
cd ~/Desktop/ivy-backend
venv_sec/bin/python -m pytest tests/test_mcp_activity_crud_client.py \
                              tests/test_mcp_activity_crud_tools.py -v

# 手動跑 MCP server（debug 用，不會接到 Claude）
IVY_MCP_USERNAME=admin IVY_MCP_PASSWORD=admin123 \
  venv_sec/bin/python -m mcp_server.activity_crud
```

stdout 走 MCP protocol，log 全走 stderr（避免 protocol 污染）。

---

## 架構摘要

```
Claude Code (stdio MCP client)
        │
        ▼
mcp_server.activity_crud.server.main()
        │  FastMCP("ivy-activity-crud")
        │  └── register_tools(mcp, IvyApiClient())
        ▼
IvyApiClient (httpx.AsyncClient + cookie jar)
        │  自動 login on first call、401 auto refresh
        ▼
ivy-backend FastAPI :8088
        │  /api/auth/login + /api/activity/{courses,supplies}*
        ▼
PostgreSQL
```

設計細節見 `~/Desktop/ivyManageSystem/docs/superpowers/specs/2026-05-17-activity-crud-mcp-design.md`。
