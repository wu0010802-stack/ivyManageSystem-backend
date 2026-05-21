# JWT Secret Rotation Runbook

設計文件：`docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md`

## 環境變數

| Env | 必填 | 用途 |
|-----|------|------|
| `JWT_SECRET_KEY` | 是 | Current secret（簽 + 驗第一順位）。長度 ≥ 32 bytes urlsafe。 |
| `JWT_SECRET_KEYS_OLDS` | 否（預設 `[]`） | JSON list of accept-only secrets。Rotation 過渡期填入舊值。 |

## 標準 rotation 流程

### 前提

- 確認應用程式版本 ≥ `feat/jwt-secret-rotation-2026-05-21-backend` merged 後的 commit。
- 確認 `JWT_ABSOLUTE_LIFETIME_HOURS`（預設 12）與 `JWT_REFRESH_GRACE_HOURS`（預設 2）的值，總共 14h 為 staff session 最長壽命。

### 步驟

**Step 1：產生新 secret**

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把輸出（例 `new_xxxx...`）暫存為 `NEW_SECRET`。

**Step 2：啟動雙 key 並存**

把 **目前** `JWT_SECRET_KEY` 的值複製進 `JWT_SECRET_KEYS_OLDS`（JSON list），把 `NEW_SECRET` 寫進 `JWT_SECRET_KEY`。例如：

```bash
# 假設原本 JWT_SECRET_KEY=old_yyyy
JWT_SECRET_KEY=new_xxxx
JWT_SECRET_KEYS_OLDS=["old_yyyy"]
```

更新所有 deployment instance 並 restart。

啟動 log 應看到：
- `_VERIFY_KEYS` 載入 2 個 kid（current + 1 old）
- 無錯誤

從此刻起：
- 新簽 token 帶 kid = `sha256(new_xxxx)[:12]`
- 舊 token（kid = `sha256(old_yyyy)[:12]`）仍可驗
- 過渡期 14h 後不應再出現舊 kid token

**Step 3：等 ≥14h（過渡期）**

實務建議等 24h（含時區與意外）。期間監控 Sentry / log 有沒有大量 `無效或過期的 Token` 401 上升 — 若有就代表還沒換完，再等。

**Step 4：清空 OLDS**

```bash
JWT_SECRET_KEYS_OLDS=[]
```

更新所有 deployment instance 並 restart。

啟動 log 應看到：
- `_VERIFY_KEYS` 只剩 1 個 kid（current）

從此刻起，任何用舊 secret 簽的 token（含外流的）都會 401 — rotation 完成。

## 緊急 rotation（secret 已外流）

**不要走軟著陸**。直接：

1. 跳到 Step 1 產生新 secret。
2. 直接寫 `JWT_SECRET_KEY=new_xxxx` + `JWT_SECRET_KEYS_OLDS=[]`（不放外流的舊值進 olds）。restart。
3. 對所有受影響 user 跑：
   ```sql
   UPDATE users SET token_version = COALESCE(token_version, 0) + 1
     WHERE is_active = true;
   ```
   ↑ 把所有 token 立即 invalidate（搭配既有 `token_version` 機制）。
4. 對外公告強制重登。

## 風險與排錯

| 症狀 | 可能原因 | 處理 |
|------|---------|------|
| 啟動 RuntimeError `JWT_SECRET_KEYS_OLDS 解析失敗` | env 值不是合法 JSON list | 檢查 JSON 格式：`'["s1","s2"]'`（雙引號內字串） |
| Step 2 後大量 401 | OLDS 漏抄、或 deploy 沒到所有 instance | 確認所有 instance restart 完成；確認 OLDS 的舊值精確等於 rotation 前的 `JWT_SECRET_KEY` |
| Step 4 後 staff 被踢一波 | 過渡期 < 14h，仍有舊 kid token 在用 | 接受（rotation 完成預期行為） / 或等更久再 Step 4 |

## Side effect：activity query token

`services/activity_query_token` 借用 `JWT_SECRET_KEY` 做 HMAC（DB 存 hex digest），未支援 multi-key。rotation 後 **既有外發 activity query token 會無法驗證通過**。

緩解：
- 計畫 rotation 前先確認沒有正在發送的家長公告含 activity URL（或接受短期失效）。
- 長期解：解耦到 `ACTIVITY_TOKEN_HMAC_KEY` env（follow-up）。
