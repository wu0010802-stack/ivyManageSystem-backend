# 物件儲存遷移設計（Supabase Storage）

- 日期：2026-05-13
- 範圍：ivy-backend（主要）、ivy-frontend（檢查 1 處 axios 行為）
- 目標：把所有使用者上傳檔從本機磁碟搬到 Supabase Storage，讓 prod container 無狀態、可水平擴展

---

## 背景

目前 backend 透過 `utils/storage.py` 把上傳檔寫到 `data/uploads/<module>/`，預設根目錄可用 `STORAGE_ROOT` 環境變數覆寫。dev 環境正常運作；但**上線後**會遇到：

1. 容器重啟磁碟資料消失（除非掛持久 volume）。
2. 無法水平擴展（多 replica 各自持有不同檔案）。
3. 沒有自動備份、版本控管。

且 prod 已採用 Supabase（DB），引入 Supabase Storage 可重用 project / 認證 / 帳號管理。

## 影響範圍

僅以下 3 個 module 把檔案寫到 `utils.storage.get_storage_path()`：

| Module | 寫入點 | 讀取點 | 公私 | 用途 |
|--------|--------|--------|------|------|
| `activity_posters` | `api/activity/settings.py` | `api/activity/public.py` | **公開** | admin 上傳活動海報、前台公開頁顯示 |
| `leave_attachments` | `api/leaves.py`、`api/portal/leaves.py` | 同 | **私有** | 假單附件（admin + 家長 portal） |
| `attendance_imports` | `api/attendance/upload.py` | （同檔處理後刪除） | **私有** | 考勤 CSV/Excel 匯入暫存 |

家長端 `parent_messages.py` / `contact_book.py` 雖有 `validate_file_signature`，目前不寫 disk，不在此次範圍。

## 設計決策

### D1. 雲端後端：Supabase Storage

採用 Supabase Storage（非 R2 / S3）。

**Why**：prod 已用 Supabase（DB 同 project），帳號/IAM/MCP 工具齊全；bucket-level RLS 可重用 PostgreSQL role；統一 source of truth。

### D2. Adapter pattern 抽象層

`utils/storage.py` 重構為 Protocol + 兩個實作：

```python
class StorageBackend(Protocol):
    def save(self, module: str, key: str, data: bytes, content_type: str) -> None: ...
    def read(self, module: str, key: str) -> bytes: ...
    def delete(self, module: str, key: str) -> None: ...
    def exists(self, module: str, key: str) -> bool: ...
    def public_url(self, module: str, key: str) -> str: ...
    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str: ...

class LocalStorage(StorageBackend): ...      # 寫 data/uploads/，回 /api/.../<file> 路徑
class SupabaseStorage(StorageBackend): ...   # 寫 bucket，回 CDN URL 或 signed URL

def get_backend() -> StorageBackend:
    """依 STORAGE_BACKEND env var 切換，預設 local。Singleton 快取。"""
```

`module` 對應 bucket 名稱（cloud）或子目錄（local），`key` 是 bucket 內路徑（含子目錄）。

### D3. 切換機制：環境變數

```bash
STORAGE_BACKEND=local                              # local | supabase，預設 local
STORAGE_ROOT=./data/uploads                        # 僅 local 用
SUPABASE_URL=https://<project>.supabase.co         # 僅 supabase 用
SUPABASE_SERVICE_ROLE_KEY=<key>                    # 僅 supabase 用（後端專用，server-side 機敏）
SUPABASE_STORAGE_SIGNED_URL_TTL=3600               # 預設 1 小時
```

`SUPABASE_SERVICE_ROLE_KEY` **絕對不可** commit、不可傳給前端。

### D4. Bucket 切分：3 個 bucket

| Bucket 名 | 權限 | 對應 module |
|-----------|------|-------------|
| `activity-posters` | **public** | `activity_posters` |
| `leave-attachments` | **private** | `leave_attachments` |
| `attendance-imports` | **private** | `attendance_imports` |

bucket-level 公私權限管理比每個物件 ACL 簡單。

### D5. 公開檔讀取：直接吐 CDN URL

`activity_posters` 改回傳 `https://<project>.supabase.co/storage/v1/object/public/activity-posters/<file>`。

前端 `posterSrc` computed 取到完整 URL，`<img :src>` 直接吃，省一跳後端。

舊端點 `GET /api/activity/public/poster/{filename}` **保留但改為 redirect 到 CDN URL**（舊海報已 in flight 的 cache、舊 client 仍能解析）。

### D6. 私有檔讀取：redirect 到 signed URL

`GET /api/portal/leaves/{leave_id}/attachments/{filename}` 與 admin 對應端點：

1. 維持原本的 JWT 權限檢查（不變）
2. 通過後 `return RedirectResponse(signed_url, status_code=302)`，TTL 1 小時
3. Supabase Storage 直接吐 bytes 給 client，後端不消耗頻寬

local backend 模式：`signed_url()` 回 `/api/.../<file>`（穿透回原 endpoint，但 endpoint 改用 `read()` 拿 bytes），實際上對 caller 透明。

### D7. DB schema：不動

`attachment_paths` 仍存「邏輯 key」字串（例如 `<leave_id>/<filename>`）。讀取時組合 `(module, key)` 換 URL。

**0 migration**。bucket 改名也不會壞 DB。

### D8. 測試：用 LocalStorage backend

既有 215+ 測試全部 `monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "uploads"))`，這些測試**完全不動**。

新增測試覆蓋：

- `LocalStorage.save/read/delete/public_url/signed_url` 行為
- `get_backend()` 依 env var 切換
- `SupabaseStorage` 用 mock storage3 client（不打真 Supabase）
- 各 router 改用 backend 後的整合測試（仍走 local backend）

不引入 Supabase 真連線測試。Service Role Key 不放 CI。

### D9. 既有 dev 資料：不遷移

dev DB 內 `activity_posters/` 跟 `leave_attachments/` 都是測試資料。prod 從零開始。

如未來 prod 需從別處遷入舊資料，另開 migration script，不在此次範圍。

### D10. 上線範圍：一次性 3 個 module

抽象層做好後，每個 router 改動約 30~50 行，沒有理由分批。

---

## 架構圖

```
┌──────────────┐                                   ┌─────────────────────────┐
│   browser    │   ──── GET poster URL ─────►      │  Supabase Storage CDN   │
│              │                                   │   (activity-posters,    │
│              │                                   │    public bucket)       │
│              │                                   └─────────────────────────┘
│              │
│              │                  ┌──────────────────────────┐
│              │ ── POST upload ──│   FastAPI backend        │
│              │                  │   (validate_file_signa-  │  ──► Supabase
│              │ ◄── 302 redirect │    ture + permission)    │      Storage
│              │     to signed    │                          │
│              │     URL          │   utils/storage.py:      │  ◄── signed_url
│              │                  │   StorageBackend         │
│              │ ──GET signed───► │                          │
│              │     URL         │  ┌────────┐  ┌─────────┐ │
└──────────────┘  Supabase直供    │  │ Local  │  │Supabase │ │
                                 │  │Storage │  │Storage  │ │
                                 │  └────────┘  └─────────┘ │
                                 └──────────────────────────┘
```

---

## 子任務分解

### Phase 1：抽象層基建
1. 改寫 `utils/storage.py`：保留 `get_storage_path()` 為 legacy alias；新增 `StorageBackend` Protocol、`LocalStorage`、`get_backend()` singleton
2. 新增 `tests/test_storage_backend.py`：覆蓋 `LocalStorage` 全部方法
3. 新增 `utils/supabase_storage.py`：`SupabaseStorage` 實作（依賴 supabase-py / storage3）
4. 加 `supabase>=2.0.0` 到 `requirements.txt`
5. 新增 mock-based test 覆蓋 `SupabaseStorage`

### Phase 2：activity_posters 改用 backend
6. `api/activity/settings.py`：`upload_activity_poster` 改用 `get_backend().save()`、把 `poster_url` 改成 `get_backend().public_url(...)`
7. `api/activity/public.py`：`get_public_poster` 改為 redirect 到公開 URL 或直接 read bytes（保留向下相容）
8. 既有 activity 測試保證綠

### Phase 3：leave_attachments 改用 backend
9. `api/leaves.py`：admin 假單附件 upload/download/delete 改用 backend
10. `api/portal/leaves.py`：家長 portal 假單附件 upload/download/delete 改用 backend；download 改 302 redirect
11. **前端 axios 驗證**：確認 redirect 到 Supabase 時 Authorization header 不被帶過去（若被帶過去會 400）。若有問題改 `<a href={signed_url}>` 或在前端 fetch 邏輯排除 Supabase host
12. 既有 leave 測試保證綠

### Phase 4：attendance_imports 改用 backend
13. `api/attendance/upload.py` 改用 backend
14. 既有 attendance 測試保證綠

### Phase 5：部署設定
15. 更新 `.env.example`：加 `STORAGE_BACKEND` / `SUPABASE_*` 變數
16. 寫部署 SOP：Supabase Dashboard 開 3 個 bucket、Zeabur env vars、SECURITY_AUDIT.md 補一條 service role key 機敏處理
17. 更新 backend `README.md` 或 `CLAUDE.md`：說明 storage backend 切換

---

## 風險與緩解

| 風險 | 緩解 |
|------|------|
| Service Role Key 外洩 | 只放 prod env var、不 commit、不傳前端；SECURITY_AUDIT.md 加 finding；rotate 頻率寫 SOP |
| Supabase Storage downtime | 接受短時故障；不做 fallback（local 模式仍可單機跑） |
| 前端 axios 把 Authorization 帶到 Supabase signed URL | Phase 3 task 11 明列為驗證點 |
| 既有測試 regression | 預設 backend=local、既有測試完全不改；CI 全綠才合併 |
| 上線時 prod DB 有舊路徑指向 local 檔 | dev → prod 從零開始；prod 若需遷舊資料另案 |

---

## 驗收條件

- `STORAGE_BACKEND=local` 模式：全部 backend 既有測試綠（≥ 815 條）
- 新增 storage backend 測試綠（local + supabase mock）
- 手動驗收：admin 後台上傳活動海報 → 前台公開頁顯示；admin/家長上傳假單附件 → 下載成功
- Service Role Key 不在 repo 任何位置（`git grep` 驗證）

---

## Non-goals

- 不做 prod 舊資料遷移工具
- 不做圖片 resize / thumbnail（之後若要可加在 backend）
- 不做 antivirus scan（既有 `validate_file_signature` 已做基本魔數驗證）
- 不更動 `parent_messages.py` / `contact_book.py` 等不寫 disk 的 router
- 不水平擴展 backend container（這次只是為了「能水平擴展」做準備）
