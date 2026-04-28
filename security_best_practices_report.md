# 後端安全檢查報告

檢查日期：2026-04-28  
範圍：`/Users/yilunwu/Desktop/ivy-backend` FastAPI 後端靜態檢查、路由權限抽查、依賴 CVE 檢查。

## 摘要

未發現可直接未授權讀取薪資、學生、家長或附件資料的高風險漏洞。舊報告列出的多數項目已在 2026-04-28 修復，包括 CSP `unsafe-eval`、JWT blocklist、Postgres rate limit、SameSite=Strict、公開端點 honeypot 與 `raise_safe_500`。

本次仍建議處理 3 個部署/供應鏈層級問題：

1. `docs` / `openapi.json` 在 production 仍預設公開，會放大 API 探測面。
2. 未設定 `TrustedHostMiddleware`，Host header 防護仰賴外層 proxy。
3. `requirements.txt` 使用全 `>=` 版本範圍，部署不可重現，升版可能引入相容性或供應鏈風險。

## Findings

### F-01：Production 預設公開 OpenAPI / Swagger 文件

- Rule ID：FASTAPI-OPENAPI-001
- Severity：Medium
- Location：`main.py:386-391`
- Evidence：
  ```python
  app = FastAPI(
      title="幼稚園考勤薪資系統",
      description="Kindergarten Payroll Management System API",
      version="2.0.0",
      lifespan=app_lifespan,
  )
  ```
  FastAPI 未設定 `docs_url=None`、`redoc_url=None`、`openapi_url=None`，因此 production 預設會提供 `/docs`、`/redoc`、`/openapi.json`。
- Impact：攻擊者可直接取得所有 API path、schema、參數與認證需求，降低探測成本。這不是繞過認證，但對內部管理系統屬於資訊洩漏放大器。
- Fix：依 `ENV` 控制 production 關閉文件：
  ```python
  app = FastAPI(
      ...,
      docs_url=None if _is_production() else "/docs",
      redoc_url=None if _is_production() else "/redoc",
      openapi_url=None if _is_production() else "/openapi.json",
  )
  ```
- Mitigation：若文件必須保留，請在 reverse proxy 加 IP allowlist 或獨立基本認證。
- False positive notes：若正式環境 proxy 已封鎖 `/docs`、`/redoc`、`/openapi.json`，實際風險會下降；但 app code 內看不到這層保護。

### F-02：App 層未啟用 Host header allowlist

- Rule ID：FASTAPI-DEPLOY baseline / TrustedHostMiddleware
- Severity：Low
- Location：`main.py:413-419`
- Evidence：
  ```python
  app.add_middleware(
      CORSMiddleware,
      allow_origins=CORS_ORIGINS,
      allow_credentials=True,
      allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
      allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
  )
  ```
  程式碼中未找到 `TrustedHostMiddleware`。
- Impact：如果部署環境沒有在 Nginx/Zeabur/Cloudflare 等外層限制 Host header，攻擊者可用任意 Host 打到 app。此 repo 目前沒有明顯用 Host 產生密碼重設連結或 redirect URL，因此評為 Low。
- Fix：加入環境變數控制的 allowlist：
  ```python
  from starlette.middleware.trustedhost import TrustedHostMiddleware

  if _is_prod_env:
      allowed_hosts = [h.strip() for h in os.environ["ALLOWED_HOSTS"].split(",")]
      app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
  ```
- Mitigation：在 reverse proxy 設定 Host allowlist，並確認非正式網域回 400/404。
- False positive notes：若平台層已保證只轉發合法 Host，這是 defense-in-depth 缺口，不是目前可直接利用的漏洞。

### F-03：依賴版本未鎖定，部署不可重現

- Rule ID：Supply-chain hardening
- Severity：Low
- Location：`requirements.txt:1-24`
- Evidence：
  ```text
  fastapi>=0.104.0
  uvicorn>=0.24.0
  sqlalchemy>=2.0.0
  ...
  python-jose>=3.3.0
  requests>=2.31.0
  httpx>=0.24.0
  ```
- Impact：每次部署可能安裝不同版本。上游套件若發布破壞性變更、惡意版本或暫時性問題，production 行為可能改變；安全修補也難以回溯「當時到底跑哪個版本」。
- Fix：使用 lock file 或 constraints，例如 `pip-tools`：
  ```bash
  pip-compile requirements.in --generate-hashes -o requirements.txt
  ```
  或維持 `requirements.in` 寫寬鬆範圍，production 用 hash-pinned `requirements.txt`。
- Mitigation：CI 固定跑 `pip-audit -r requirements.txt`，部署 artifact 保留 `pip freeze`。
- False positive notes：這不是單一可利用漏洞；它是供應鏈與可重現部署風險。

## 已檢查且未發現重大問題

- JWT：`utils/auth.py` 有 alg 檢查、短效 token、`jti`、blocklist、`token_version`、停用帳號檢查。
- Cookie：`utils/cookie.py` 預設 `HttpOnly`、production `Secure`、`SameSite=Strict`。
- CORS：`main.py` production 缺 `CORS_ORIGINS` 會拒絕啟動，未使用 `*` 搭配 credentials。
- 公開活動端點：海報下載有檔名白名單與 `relative_to` 防穿越；報名/查詢已有 honeypot、rate limit、延遲與 safe 500。
- 檔案附件：portfolio 下載透過 DB key 反查與權限 scope，local storage 也檢查 root prefix。
- SQL：抽查 `text()` / `execute()` 皆使用參數或固定 SQL，未看到 user input 直接拼接。
- 路由權限：管理端主要路由使用 `require_staff_permission` / `require_permission`；家長端使用 `require_parent_role` 與資源 ownership 檢查。
- 依賴 CVE：已執行 `python3 -m pip_audit -r requirements.txt`，結果為 `No known vulnerabilities found`。

## 建議優先順序

1. F-01：關閉或保護 production API docs。
2. F-02：加入 `TrustedHostMiddleware` 或確認 proxy 層 Host allowlist。
3. F-03：拆出 `requirements.in`，產生 hash-pinned lock 檔，讓部署可重現。

## 限制

本次是程式碼層靜態審查與依賴掃描，未做實際滲透測試、DAST、資料庫權限檢查、備份加密檢查、Cloudflare/Nginx/Zeabur 設定檢查。
