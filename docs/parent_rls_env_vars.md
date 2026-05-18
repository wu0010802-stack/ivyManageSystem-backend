# Phase 0 env vars 手動套用清單

Hook 擋 `.env*` 編輯，這是給 user 手動套到 `.env.example` 的內容。

## 在 `.env.example` 第 5 行（`DATABASE_URL=...`）後插入：

```bash
# 家長端 RLS engine（Phase 0 後可選；Phase 1+ 啟用 RLS 時必須）
# 設定後 api/parent_portal/* 會用受 Row-Level Security 約束的連線。
# 部署步驟：
#   1. 跑 `alembic upgrade head`（會建 ivy_parent_login / ivy_admin_login 等 4 個 role）
#   2. 在 PG 內 `ALTER ROLE ivy_parent_login PASSWORD '<from-secret>';`
#   3. 填回下列兩變數，重啟 ivy-backend
# 未設兩變數時 get_parent_engine() return None；
# Phase 1+ router 拋 RuntimeError 而非 fallback。
# PARENT_DB_USER=ivy_parent_login
# PARENT_DB_PASSWORD=
```

## 在 dev `.env` 也加同樣兩行（含實際 dev 密碼）：

```bash
PARENT_DB_USER=ivy_parent_login
PARENT_DB_PASSWORD=dev_parent_pw_2026_05_18
```

dev 密碼已透過 `ALTER ROLE ivy_parent_login PASSWORD 'dev_parent_pw_2026_05_18'`
寫入本機 PG（migration 跑完後 spike session 內設的）。Prod 部署時務必改為強密碼。
