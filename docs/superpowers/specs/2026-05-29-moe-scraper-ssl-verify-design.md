# MOE 爬蟲 SSL 驗證修復 + 誠實 UA

**日期**：2026-05-29
**範圍**：純後端，單一模組 `services/moe_kindergarten_scraper.py` + 一張 bundled 憑證 + 測試
**對應 audit finding**：P1 #15「爬蟲 SSL 驗證關閉 + 偽裝 Chrome UA」

## 問題

`services/moe_kindergarten_scraper.py` 的 `_make_session()` 設 `sess.verify = False`，
完全停用 TLS 憑證驗證 → 失去 MITM 防護。攻擊者可在出 NAT 點注入假競品資料，
影響園長決策。同 session 還偽裝成桌面 Chrome UA。

### 根因（實測確認，非推測）

- MOE 站 `ap.ece.moe.edu.tw` 憑證由 **TWCA**（台灣網路認證）簽發，鏈為
  `leaf → TWCA Secure SSL Certification Authority（中繼）→ TWCA Global Root CA`。
- **`TWCA Global Root CA` 已在 certifi**（根被信任）。
- **失敗主因是 server 只回傳 leaf、漏送中繼憑證** → 預設驗證報
  `unable to get local issuer certificate`。
- DGPA 用的「放寬 X509 strict flag」對 MOE **無效**（實測仍 CERTIFICATE_VERIFY_FAILED），
  因為這不是 strict flag 問題，是鏈斷。

驗證證據：把中繼憑證 `load_verify_locations` 進 context（保留 check_hostname +
CERT_REQUIRED）後，MOE 回 **200 / 49KB HTML**。

## 解法

1. **Bundle 中繼憑證** `services/certs/twca_secure_ssl_ca.pem`
   - 來源 AIA：`http://sslserver.twca.com.tw/cacert/secure_sha2_2023G3.crt`
   - subject：`TWCA Secure SSL Certification Authority`；有效至 **2030-10-16**
   - 檔頭含 provenance 註解（來源 / 下載日 / 到期 / SHA256 / 刷新步驟）

2. **`_MoeSSLAdapter`**（仿既有 `official_calendar._DgpaSSLAdapter`）
   - `ssl.create_default_context(cafile=certifi.where())` + `load_verify_locations(中繼)`
   - `check_hostname=True` + `CERT_REQUIRED`
   - PEM 路徑 `Path(__file__).parent / "certs" / ...`（scheduler context CWD 不可預測）
   - **fail-closed**：PEM 遺失 → 記明確錯誤日誌（提示 AIA 刷新 URL）並 raise，
     **絕不** fallback 到 `verify=False`

3. **`_make_session()`**
   - 移除 `sess.verify = False`
   - 只把 adapter mount 到 `https://ap.ece.moe.edu.tw`
   - `kiang.github.io` 兩個 URL 走預設嚴格驗證（憑證有效，無需特殊處理）
   - UA 改誠實識別字串（不含 `Mozilla`/`Chrome`；實測 MOE 對非瀏覽器 UA 回 200）
   - 移除頂端 `urllib3.disable_warnings(InsecureRequestWarning)` 與誤導註解

## 測試（全離線、無網路）

- **安全屬性（核心回歸）**：`_make_session().verify` 永不為 `False`；MOE adapter 的
  context 一定 `CERT_REQUIRED` + `check_hostname=True`。
- MOE host 走 `_MoeSSLAdapter`；github.io 走預設 adapter（嚴格驗證）。
- UA 為誠實字串（assert 不含 `Mozilla`/`Chrome`、含 `ivyManageSystem`）。
- **憑證到期守衛（CI 提前報警）**：bundled PEM 能解析、subject 正確、
  `not_valid_after` > now + 45 天 → 憑證快到期時 CI 先 fail，留刷新時間。

## 明確不做

- `recruitment_ivykids_sync.py` 的 UA：保持不動（`ivykids.tw` 是自家站、
  無法測 WAF/login、ToS 不適用）。
- `kiang.github.io` punish 資料的 schema/sha 驗證：另屬 P2 資料品質議題。

## 流程

worktree off `origin/main`（`fix/moe-scraper-ssl-verify-2026-05-29-backend`）→
TDD（先寫回歸測試）→ 後端一個 PR。
