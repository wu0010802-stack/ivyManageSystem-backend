# Dependabot Rollout — 兩 repo 依賴自動監控與 patch auto-merge

**日期**：2026-05-28
**狀態**：Design（pending review）
**Scope**：跨 repo（ivy-backend + ivy-frontend）
**前置任務**：P2 audit finding 19（依賴版本管理寬鬆）
**相關**：finding 17/18/20 為獨立 sub-project，與本 spec 不耦合
**工時估**：0.5 天

---

## 1. 動機

### 1.1 既有狀況

- `ivy-backend/requirements.txt`（30 dep）：全用 `>=`，無 lock file；2 條已 cap 上限（`pydantic-settings<3.0` / `redis<6` / `fakeredis<3`）
- `ivy-frontend/package.json`（33 dep）：全用 `^`（caret，允許 minor 升級）；有 `package-lock.json` 但無自動監控
- 兩 repo 皆未設 `.github/dependabot.yml` 或 `renovate.json`
- 既有 CI 含 `pip-audit`（backend）與 `npm audit`（frontend）做 CVE 守門，但**只在 PR 時跑**，無主動掃描；CVE 公布後可能延遲數週才被注意到

### 1.2 想解決的問題

- **CVE 反應慢**：依賴新 CVE 公布後，無人主動拉新版本；需 audit 出貨才補（finding 19、近期已 5 條 CVE 待修）
- **手動升級噪音**：累積數十個 patch/minor 等手動處理，易遺漏
- **`>=` 無上限風險**：環境飄移；新環境 `pip install` 可能拉到不相容版本（如 Python 3.10+ only 的 dep）

### 1.3 不做什麼（YAGNI）

- **不導 `pip-tools` / `uv` lock file**：屬獨立 sub-project，本次只用 `>=` + Dependabot 監控 + branch protection 達 95% 效果
- **不改 Renovate**：Dependabot 內建於 GitHub 已足夠，第三方 App overkill
- **不自動處理 minor/major**：對 framework 級（fastapi/sqlalchemy/pydantic/vue/element-plus）minor 都可能 breaking；保守只 auto-merge patch
- **不設 branch protection**：屬 user 手動 GitHub UI 操作，列為 prerequisite

---

## 2. 範圍與整體架構

```
ivy-backend/
├── .github/
│   ├── dependabot.yml             ← 新檔
│   └── workflows/
│       └── dependabot-auto-merge.yml   ← 新檔
├── requirements.txt               ← 修改：4 條補上限 cap

ivy-frontend/
├── .github/
│   ├── dependabot.yml             ← 新檔
│   └── workflows/
│       └── dependabot-auto-merge.yml   ← 新檔
```

兩 repo 結構對稱，便於日後維護。

---

## 3. Backend Dependabot 設定

### 3.1 `ivy-backend/.github/dependabot.yml`

```yaml
version: 2
updates:
  # Python pip dependencies
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "09:00"
      timezone: "Asia/Taipei"
    open-pull-requests-limit: 10
    labels: ["dependencies", "backend"]
    commit-message:
      prefix: "chore(deps)"
      include: "scope"
    groups:
      pytest-suite:
        patterns: ["pytest*"]
      sqlalchemy-alembic:
        patterns: ["sqlalchemy", "alembic"]
      pillow:
        patterns: ["Pillow", "pillow-heif"]
      sentry:
        patterns: ["sentry-sdk*"]
    ignore:
      # Python 3.9 上限保護（升 3.10 後移除此 ignore section）
      - dependency-name: "python-multipart"
        versions: [">=0.0.27"]
      - dependency-name: "Pillow"
        versions: [">=12.2.0"]
      - dependency-name: "python-dotenv"
        versions: [">=1.2.2"]
      - dependency-name: "requests"
        versions: [">=2.33.0"]
      # 已知 supply-chain 攻擊版本（與 requirements.txt `!=0.136.3` 對齊）
      - dependency-name: "fastapi"
        versions: ["0.136.3"]

  # GitHub Actions third-party action 監控
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "monthly"
    labels: ["dependencies", "ci"]
    commit-message:
      prefix: "chore(ci)"
```

**設計理由：**

- **schedule weekly Monday 09:00 Taipei**：避開週末，週一早上 review 排程；既有 CI cron 用 Taipei（`dr-backup.yml` 02:17+08），時區一致
- **groups**：把通常一起升的合 1 PR
  - `pytest*` 涵蓋 pytest / pytest-cov（同生態）
  - `sqlalchemy` + `alembic` 嚴重耦合，從不單升
  - `Pillow` + `pillow-heif` 必同步
  - `sentry-sdk[fastapi]` 主從一致
- **ignore (Python 3.9 cap)**：CLAUDE.md / requirements.txt comment 已明列 4 條，Dependabot 必須 mirror，否則每週發 PR 你必須關閉，噪音
- **github-actions monthly**：third-party action 通常 patch-only 升級，month cadence 足夠

### 3.2 `ivy-backend/requirements.txt` 修改

新增上限 cap 與 dependabot.yml `ignore` 形成雙保險（防新環境 `pip install` 飄移）：

```diff
-python-multipart>=0.0.20  # CVE-2026-40347, CVE-2026-42561（>=0.0.27 要求 Python 3.10+，本機用 3.9）
+python-multipart>=0.0.20,<0.0.27  # CVE-2026-40347, CVE-2026-42561; <0.0.27 需 Python 3.10+

-Pillow>=11.3.0  # CVE-2026-25990, CVE-2026-40192, CVE-2026-42308/9/10/11（>=12.2.0 要求 Python 3.10+，本機用 3.9）
+Pillow>=11.3.0,<12.2.0  # CVE-2026-25990, CVE-2026-40192, CVE-2026-42308/9/10/11; <12.2.0 需 Python 3.10+

-python-dotenv>=1.2.0  # CVE-2026-28684（>=1.2.2 要求 Python 3.10+，本機用 3.9）
+python-dotenv>=1.2.0,<1.2.2  # CVE-2026-28684; <1.2.2 需 Python 3.10+

-requests>=2.32.0  # CVE-2026-25645（>=2.33.0 要求 Python 3.10+，本機用 3.9）
+requests>=2.32.0,<2.33.0  # CVE-2026-25645; <2.33.0 需 Python 3.10+
```

其他 26 行不動（保留 `>=` 讓 Dependabot 偵測新版本）。

---

## 4. Frontend Dependabot 設定

### 4.1 `ivy-frontend/.github/dependabot.yml`

```yaml
version: 2
updates:
  # npm dependencies
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "09:00"
      timezone: "Asia/Taipei"
    open-pull-requests-limit: 10
    labels: ["dependencies", "frontend"]
    commit-message:
      prefix: "chore(deps)"
      include: "scope"
    groups:
      fullcalendar:
        patterns: ["@fullcalendar/*"]
      sentry:
        patterns: ["@sentry/*"]
      vue-core:
        patterns: ["vue", "vue-router", "pinia"]
      vitest-suite:
        patterns: ["vitest", "@vitest/*", "@vue/test-utils"]
      typescript-tooling:
        patterns: ["typescript", "vue-tsc", "@vue/tsconfig", "@types/*"]

  # GitHub Actions third-party action 監控
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "monthly"
    labels: ["dependencies", "ci"]
    commit-message:
      prefix: "chore(ci)"
```

**設計理由：**

- **無 `ignore` section**：前端無 Python 3.9 對等限制；若日後 Vite/Vue major bump 不相容，再加
- **groups**：
  - `@fullcalendar/*` 7 個 sub-package（core/daygrid/interaction/list/timegrid/vue3）必須同步升
  - `@sentry/*` Sentry 主從必同步（`@sentry/vue` + `@sentry/vite-plugin`）
  - `vue-core` 三件套（vue / vue-router / pinia）官方推薦同步
  - `vitest-suite` Vitest 全家 + test-utils
  - `typescript-tooling` TS 編譯器與相關 type 包

### 4.2 `ivy-frontend/package.json` 不修改

`^` semver 已有上限隱含（caret 不跨 major），無需動。

---

## 5. Auto-merge Workflow（兩 repo）

### 5.1 `ivy-backend/.github/workflows/dependabot-auto-merge.yml`

```yaml
name: Dependabot Auto-merge (patch only)

on:
  pull_request_target:
    branches: [main]

permissions:
  contents: write
  pull-requests: write

jobs:
  auto-merge:
    if: github.actor == 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - name: Fetch Dependabot metadata
        id: meta
        uses: dependabot/fetch-metadata@v2
        with:
          github-token: "${{ secrets.GITHUB_TOKEN }}"

      - name: Enable auto-merge (squash) on patch only
        if: steps.meta.outputs.update-type == 'version-update:semver-patch'
        run: gh pr merge --auto --squash "$PR_URL"
        env:
          PR_URL: ${{ github.event.pull_request.html_url }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### 5.2 `ivy-frontend/.github/workflows/dependabot-auto-merge.yml`

內容與 backend 完全相同（不需差異化）。

**設計理由：**

- **`pull_request_target`** (vs `pull_request`)：可拿到 `GITHUB_TOKEN` write 權限，Dependabot PR 來自 fork-like 上下文，普通 `pull_request` event token 是 read-only
- **`if: github.actor == 'dependabot[bot]'`**：避免人為觸發 auto-merge
- **`--auto`**：條件式合（CI 全綠後才真合，不會強推紅 PR）
- **`--squash`**：保持 main 線性，與既有 squash policy 對齊
- **依賴 branch protection**：必須 user 在 GitHub repo settings → Branches → main 啟用 "Require status checks to pass before merging" + 勾 pytest/typecheck/lint 等 required checks；否則 `--auto` 變空白許可
- **⚠️ `pull_request_target` 安全約束**：本 workflow **不可**新增 `actions/checkout` 拉 PR head SHA + 跑 PR 內 untrusted code（如 `npm test`、`pytest`），否則攻擊者可送 malicious PR 在 main context 偷 secrets。本 workflow 只用 `dependabot/fetch-metadata` 讀 PR metadata + `gh pr merge` 觸發合併，無拉 PR code，安全。日後若需擴功能（如「auto-revert 失敗 PR」），必須改用 `pull_request` event 並接受 token 受限

---

## 6. 行為變更與 user 影響

### 6.1 啟用後一週的預期 PR 數

- Backend：估計 5-10 個首輪 PR（pytest 9.0.3→9.0.x、sqlalchemy 2.0.x、redis 5.x、sentry-sdk 2.x 等）
- Frontend：估計 8-15 個首輪 PR（@fullcalendar 全家、@sentry 全家、vitest、typescript 等）
- 之後穩定到每週 1-3 個 PR / repo

### 6.2 對 user workflow 的影響

- **+** CVE 反應從 weeks → days（patch CVE 週內 auto-merge）
- **+** Dependabot security alert 與升級 PR 一條龍
- **-** 每週需 review minor/major PR（不過量；本來也應每週看一眼）
- **-** 啟用後第一週 PR 噪音較大（重 catch-up），之後穩定

### 6.3 預設關閉的安全網

- 若某 patch PR 意外 break CI，`--auto` 不會強推（CI 紅就卡住）
- 若 dependabot.yml `ignore` 未涵蓋的 dep 升級 break runtime，traditional rollback：`git revert` PR

---

## 7. Prerequisites（user 手動操作）

啟用前 user 需在 GitHub UI 完成：

1. **兩 repo branch protection（main）**：Settings → Branches → Branch protection rules → main：
   - Require status checks to pass before merging：勾選
   - 加 required checks（job 名稱對齊 `.github/workflows/ci.yml`）：
     - Backend：
       - `依賴 CVE 掃描（HIGH-1）`
       - `Ruff DTZ Lint Gate`
       - `Tests (TZ=Asia/Taipei)`（matrix job 1）
       - `Tests (TZ=UTC)`（matrix job 2；目前 `continue-on-error: true` 屬觀察期，先不列 required，等 datetime-taipei 落地後再加）
       - `OpenAPI Drift Check`
       - `Centralized Settings Gate`
       - `Money Rounding Gate (round_half_up)`
       - `Alembic Single Head Gate`
       - `Alembic Roundtrip`
       - `Alembic Symmetry Lint`
       - `Notification Dispatch Gate (forbid line_service.notify_*)`
     - Frontend：
       - `依賴 CVE 掃描（HIGH-1）`
       - `Tests & Build`（含 vitest + typecheck + build 內部 step）
       - `OpenAPI Drift Check`
   - Require branches to be up to date before merging：可選（建議勾）
2. **Dependabot security alerts**：Settings → Code security and analysis → Dependabot alerts：Enable
3. **Dependabot version updates**：Settings → Code security and analysis → Dependabot version updates：Enable
4. **Dependabot 權限對 workflows**：Settings → Actions → General → Workflow permissions：勾 "Read and write permissions" + "Allow GitHub Actions to create and approve pull requests"

如 user 未完成 1 + 4，auto-merge workflow 即使 deploy 也無實際效果（夯在 pending 狀態）。

---

## 8. 測試與驗收

### 8.1 自動驗證

無新增 pytest / vitest（純 GitHub config + workflow，不入 application code）。

### 8.2 手動驗收（PR 合入後）

1. **dependabot.yml 解析 OK**：兩 repo 各到 Settings → Code security → Dependabot → "Recent updates" 應顯示 "Updates checked"，無 yaml syntax error
2. **首輪 PR 啟動**：repo Settings → Code security → Dependabot → Manage → "Check for updates"，1-2 分鐘內可見 PR 生出（或等 Monday 09:00 自動跑）
3. **auto-merge workflow 解析 OK**：到 PR Actions tab 看 "Dependabot Auto-merge (patch only)" workflow run，patch update 應 "Enable auto-merge" step 成功；minor/major 應 skip
4. **CI 全綠 PR 自動合**：選一個 patch PR（CI 全綠），1-2 分鐘內 PR status 變 "Squash and merge - waiting for checks" → 全綠後自動 merge

### 8.3 驗收失敗 troubleshooting

- `dependabot.yml` parse error：GitHub UI 會顯示具體 yaml line/column，照 schema fix
- workflow 沒跑：檢查 step 7.4 workflow permissions 是否設對
- `--auto` 一直 pending：檢查 branch protection 是否設 required checks（沒設 → `--auto` 等於不啟用）

---

## 9. Out of Scope（follow-up sub-projects）

以下不在本 spec 範圍，列為後續：

| Follow-up | 屬性 | 何時做 |
|---|---|---|
| `pip-tools` / `uv` lock file | reproducibility 增強 | Python 3.10 遷移後 |
| Renovate 取代 Dependabot | 更靈活 packageRules | 若 Dependabot 出貨後 6 個月仍覺不足 |
| Sentry / Slack 通知 Dependabot PR | 通知整合 | 若 user 漏 review minor PR 累積 |
| Docker base image scan | Trivy / Snyk | 若導入 Docker prod 部署 |
| Branch protection automation (Terraform) | IaC | 若多 repo 擴張 |

---

## 10. 風險與回退

### 10.1 主要風險

- **Branch protection 未設 → auto-merge 失控**：mitigation 是 §7 prerequisite 強制
- **Python 3.9 ignore 漏設 → 紅 CI PR 灌爆**：mitigation 是 §3.1 ignore 4 條已列；新發現可即時補
- **fullcalendar/sentry group 升級 break 既有元件**：mitigation 是這類 group PR 視同 minor 必手審，不 auto-merge

### 10.2 回退方式

- **完整回退**：`git revert` Dependabot 設定 PR；Dependabot 立即停止
- **單項 dep 回退**：在 dependabot.yml `ignore` 加該 dep + 對應版本範圍
- **暫停升級**：repo Settings → Code security → Dependabot version updates → Disable

---

## 11. 預估與分工

- **規模**：4 個新檔（2 repo × 2 檔）+ 1 個檔修改（requirements.txt 4 行）
- **工時**：0.5 天（含 spec / plan / commit / push / user 手動 prerequisite）
- **PR 數**：2（backend 1 + frontend 1）
- **依賴**：無
- **block 任何後續 sub-project？** 不 block；可獨立先 ship
