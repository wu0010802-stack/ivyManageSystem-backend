# 上傳檔案儲存部署 SOP

## 本地開發（預設）

不需任何設定。`STORAGE_BACKEND` 預設 `local`，檔案寫到 `backend/data/uploads/`。

## .env.example 須手動補上（hook 阻擋 AI 修改）

請在 `.env.example` 末尾追加以下內容（複製貼上即可）：

```bash
# ===== 上傳檔案儲存設定 =====
# STORAGE_BACKEND: 切換上傳檔儲存後端
#   local（預設）= 寫到本機 STORAGE_ROOT，dev 環境使用
#   supabase     = 寫到 Supabase Storage，prod 環境使用
STORAGE_BACKEND=local

# 僅 local backend 用：上傳檔根目錄（預設 backend/data/uploads/）
# STORAGE_ROOT=/var/lib/ivy/uploads

# 僅 supabase backend 用：
# SUPABASE_URL=https://<your-project>.supabase.co
# SUPABASE_SERVICE_ROLE_KEY=<service-role-key>   # 機敏！僅後端使用，絕對不可外洩
# SUPABASE_STORAGE_SIGNED_URL_TTL=3600           # 私有檔 signed URL 有效秒數
```

## 上線（Supabase Storage）

### 1. 建立 Supabase Storage buckets

登入 Supabase Dashboard → Storage → New bucket，建立以下 3 個 bucket：

| Bucket name | Public | Purpose |
|-------------|--------|---------|
| `activity-posters`     | ✅ Public  | 活動海報，前台直接從 CDN 抓 |
| `leave-attachments`    | ❌ Private | 假單附件，後端發 signed URL |
| `attendance-imports`   | ❌ Private | 考勤匯入暫存，僅後端短暫使用 |
| `growth-reports`       | ❌ Private | 學生成長報告 PDF，後端發 signed URL |

或使用 Supabase CLI / MCP `supabase` server 自動建。

### 2. 取得 Service Role Key

Supabase Dashboard → Project Settings → API → service_role key

⚠ **這把 key 等同 root 權限。絕對不可：**
- commit 到任何 repo
- 傳給前端
- 寫到日誌
- 分享到 Slack / email

### 3. 設定 backend env vars（以 Zeabur 為例）

Service Settings → Environment Variables 加：

```bash
STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
SUPABASE_STORAGE_SIGNED_URL_TTL=3600
```

`STORAGE_ROOT` 不設（supabase 模式不用）。

### 4. 部署後驗證

1. admin 上傳活動海報 → 前台公開頁顯示 → 檢查圖片 URL 是 `https://<project>.supabase.co/...`
2. 教師 portal 上傳假單附件 → 下載 → 檢查 302 redirect 到 signed URL（帶 token）
3. admin 上傳考勤 Excel → 確認解析成功

### 5. 切換回 local（回滾）

若 Supabase Storage 出問題，可暫時改 `STORAGE_BACKEND=local`，container 必須掛 `/var/lib/ivy/uploads` 持久 volume。
注意：切換後既有 DB 內 `poster_url`、`attachment_paths` 指向的物件還在 Supabase，回 local 後找不到 → 必須先把雲端檔搬下來（人工 `supabase storage download`）。**這條切換不是無縫的**。

另有 R2 異地鏡像 `ivy-dr/storage/`，可用 `aws s3 cp ... --endpoint-url=$R2_ENDPOINT` 拉回後再 `supabase storage upload` 回填到新 bucket（見 dr-runbook.md §6 Path B Step 5）。

## Service Role Key 輪替

建議每 90 天輪替一次：
1. Dashboard → Reset service_role key
2. 更新 prod env var
3. Restart backend service

舊 key 立即失效，container 重啟生效，無 client-side 衝擊（service key 只在後端）。
