# Dependabot Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 兩 repo 啟用 GitHub Dependabot 監控依賴更新；patch 自動合、minor/major 手審；backend requirements.txt 補 4 條 Python 3.9 上限 cap。

**Architecture:** 純 GitHub config + workflow，無 application code。兩 repo 對稱：各加 1 個 `dependabot.yml` + 1 個 `dependabot-auto-merge.yml` workflow。Backend 額外修改 `requirements.txt`（4 行 cap）與 `dependabot.yml` 內 `ignore` section 對齊（雙保險，防新環境 `pip install` 飄移到 Python 3.10+ only 版本）。

**Tech Stack:** GitHub Dependabot (built-in)、GitHub Actions、Python 3.9（backend 既有版本，限制升級範圍）。

**Spec:** `ivy-backend/docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md`

**重要 context：**
- Backend `git status` 已有 user 並行 WIP（`api/overtimes.py` / `pyproject.toml` / `schemas/*` / `uv.lock` 等），**不要 stash、不要 `git add -A`**，只 `git add` 本 plan 列的具體檔案路徑。
- Frontend working tree 狀態執行時再查。
- 兩 repo 各開一條 feature branch，分別開兩個 PR。
- 本 plan 完成 = 兩 PR 合入 main + user 收到 §7 prerequisite checklist；GitHub UI 手動 prerequisite 屬 user 操作，不在 plan 範圍。

---

## File Structure

```
ivy-backend/
├── .github/
│   ├── dependabot.yml                                ← Task 1 (Create)
│   └── workflows/
│       └── dependabot-auto-merge.yml                 ← Task 2 (Create)
└── requirements.txt                                  ← Task 3 (Modify 4 lines)

ivy-frontend/
├── .github/
│   ├── dependabot.yml                                ← Task 5 (Create)
│   └── workflows/
│       └── dependabot-auto-merge.yml                 ← Task 6 (Create)
```

---

## Task 1: Backend `.github/dependabot.yml`

**Files:**
- Create: `ivy-backend/.github/dependabot.yml`
- Branch: `chore/dependabot-rollout-2026-05-28-backend`

- [ ] **Step 1: Create feature branch (在 ivy-backend repo)**

```bash
cd ~/Desktop/ivy-backend
git fetch origin
git checkout -b chore/dependabot-rollout-2026-05-28-backend origin/main
```

Expected: 切到新 branch，working tree 仍保有 user WIP（uncommitted），不要 commit 那些。

- [ ] **Step 2: Verify `.github/` directory exists**

```bash
ls ~/Desktop/ivy-backend/.github/
```

Expected: 看到 `workflows/`。若不存在則 `mkdir -p ~/Desktop/ivy-backend/.github`。

- [ ] **Step 3: Create `dependabot.yml`**

寫入 `~/Desktop/ivy-backend/.github/dependabot.yml`（用 Write 工具，整檔內容）：

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
    labels:
      - "dependencies"
      - "backend"
    commit-message:
      prefix: "chore(deps)"
      include: "scope"
    groups:
      pytest-suite:
        patterns:
          - "pytest*"
      sqlalchemy-alembic:
        patterns:
          - "sqlalchemy"
          - "alembic"
      pillow:
        patterns:
          - "Pillow"
          - "pillow-heif"
      sentry:
        patterns:
          - "sentry-sdk*"
    ignore:
      # Python 3.9 上限保護（升 Python 3.10 後移除此 section）
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
    labels:
      - "dependencies"
      - "ci"
    commit-message:
      prefix: "chore(ci)"
```

- [ ] **Step 4: Validate YAML syntax locally**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`。若 syntax error 會 raise `yaml.scanner.ScannerError`。

- [ ] **Step 5: Commit (不要 `git add -A`)**

```bash
cd ~/Desktop/ivy-backend
git add .github/dependabot.yml
git status --short
```

Expected: `git status --short` 只有 `A  .github/dependabot.yml` 一行屬本次新增；user WIP（`M api/overtimes.py` 等）仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
chore(ci): 加入 Dependabot 監控 pip + github-actions

每週一 09:00 Asia/Taipei 掃描；4 個 group 合併常一起升的 dep；
ignore 列 Python 3.9 cap dep + fastapi 0.136.3 supply-chain 版。

Refs: docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Backend Auto-merge Workflow

**Files:**
- Create: `ivy-backend/.github/workflows/dependabot-auto-merge.yml`
- Branch: 同 Task 1（`chore/dependabot-rollout-2026-05-28-backend`）

- [ ] **Step 1: Confirm on correct branch**

```bash
cd ~/Desktop/ivy-backend
git branch --show-current
```

Expected: `chore/dependabot-rollout-2026-05-28-backend`

- [ ] **Step 2: Create workflow file**

寫入 `~/Desktop/ivy-backend/.github/workflows/dependabot-auto-merge.yml`：

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

⚠️ **安全約束**：本 workflow **不可**新增 `actions/checkout` 拉 PR head SHA + 跑 PR 內 untrusted code（如 `npm test`、`pytest`），否則攻擊者可送 malicious PR 在 main context 偷 secrets。本 workflow 只用 `dependabot/fetch-metadata` 讀 PR metadata + `gh pr merge` 觸發合併，無拉 PR code，安全。

- [ ] **Step 3: Validate YAML**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/dependabot-auto-merge.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 4: Commit (只 add 本檔)**

```bash
cd ~/Desktop/ivy-backend
git add .github/workflows/dependabot-auto-merge.yml
git status --short
```

Expected: `A  .github/workflows/dependabot-auto-merge.yml` 一行屬本次。

```bash
git commit -m "$(cat <<'EOF'
chore(ci): Dependabot patch PR auto-merge workflow

只 auto-merge patch update（`update-type == 'version-update:semver-patch'`），
minor/major 需手審。依賴 GitHub branch protection required checks 全綠
才會真合（--auto 等狀態）。

Refs: docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Backend `requirements.txt` 補上限 cap

**Files:**
- Modify: `ivy-backend/requirements.txt`（4 行）
- Branch: 同 Task 1

- [ ] **Step 1: Confirm on correct branch**

```bash
cd ~/Desktop/ivy-backend
git branch --show-current
```

Expected: `chore/dependabot-rollout-2026-05-28-backend`

- [ ] **Step 2: Read current state of requirements.txt**

用 Read 工具讀 `~/Desktop/ivy-backend/requirements.txt` 整檔，確認下列 4 行原文（避免行尾 comment 不一致）：

```
python-multipart>=0.0.20  # CVE-2026-40347, CVE-2026-42561（>=0.0.27 要求 Python 3.10+，本機用 3.9）
Pillow>=11.3.0  # CVE-2026-25990, CVE-2026-40192, CVE-2026-42308/9/10/11（>=12.2.0 要求 Python 3.10+，本機用 3.9）
python-dotenv>=1.2.0  # CVE-2026-28684（>=1.2.2 要求 Python 3.10+，本機用 3.9）
requests>=2.32.0  # CVE-2026-25645（>=2.33.0 要求 Python 3.10+，本機用 3.9）
```

- [ ] **Step 3: Edit 4 lines**

用 Edit 工具，分 4 次 single-line replacement（不要用 sed/awk，避免 BSD/GNU 差異與多行 anchor 問題）：

第 1 行：
- old: `python-multipart>=0.0.20  # CVE-2026-40347, CVE-2026-42561（>=0.0.27 要求 Python 3.10+，本機用 3.9）`
- new: `python-multipart>=0.0.20,<0.0.27  # CVE-2026-40347, CVE-2026-42561; <0.0.27 需 Python 3.10+`

第 2 行：
- old: `Pillow>=11.3.0  # CVE-2026-25990, CVE-2026-40192, CVE-2026-42308/9/10/11（>=12.2.0 要求 Python 3.10+，本機用 3.9）`
- new: `Pillow>=11.3.0,<12.2.0  # CVE-2026-25990, CVE-2026-40192, CVE-2026-42308/9/10/11; <12.2.0 需 Python 3.10+`

第 3 行：
- old: `python-dotenv>=1.2.0  # CVE-2026-28684（>=1.2.2 要求 Python 3.10+，本機用 3.9）`
- new: `python-dotenv>=1.2.0,<1.2.2  # CVE-2026-28684; <1.2.2 需 Python 3.10+`

第 4 行：
- old: `requests>=2.32.0  # CVE-2026-25645（>=2.33.0 要求 Python 3.10+，本機用 3.9）`
- new: `requests>=2.32.0,<2.33.0  # CVE-2026-25645; <2.33.0 需 Python 3.10+`

- [ ] **Step 4: Verify diff is exactly 4 lines changed**

```bash
cd ~/Desktop/ivy-backend
git diff --stat requirements.txt
```

Expected: `requirements.txt | 8 ++++----`（4 deletion + 4 insertion）

```bash
git diff requirements.txt
```

Expected: 看到 4 處 `-/+` 對應上面 4 條改動，**沒有其他行被動到**。

- [ ] **Step 5: Verify pip can still resolve**

```bash
cd ~/Desktop/ivy-backend
python3 -m pip install --dry-run --quiet -r requirements.txt 2>&1 | tail -20
```

Expected: 無 ERROR；最後可能顯示 `Would install ...` 列表。若有 `ERROR: Could not find a version that satisfies the requirement ...` 則 cap 錯了，回 Step 3 重 review。

⚠️ 若 user 沒 venv 環境此 step 會失敗，跳過此 step（本機驗證 nice-to-have，CI 會跑真正 pip install）。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add requirements.txt
git status --short
```

Expected: `M  requirements.txt` 一行屬本次；user WIP 其他檔仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
chore(deps): requirements.txt 補 Python 3.9 上限 cap

4 條 dep 已知 next minor 要求 Python 3.10+：
- python-multipart <0.0.27
- Pillow <12.2.0
- python-dotenv <1.2.2
- requests <2.33.0

與 .github/dependabot.yml ignore section 對齊，雙保險防新環境
pip install 飄移。本機 Python 3.9 → 3.10 升級後可移除。

Refs: docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §3.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Push Backend Branch & Open PR

**Files:** 無新增；操作 GitHub 遠端

- [ ] **Step 1: Confirm 3 commits on branch**

```bash
cd ~/Desktop/ivy-backend
git log --oneline origin/main..HEAD
```

Expected: 3 行 commit，順序為 dependabot.yml → workflow → requirements.txt。

- [ ] **Step 2: Push branch to origin**

```bash
cd ~/Desktop/ivy-backend
git push -u origin chore/dependabot-rollout-2026-05-28-backend
```

Expected: `Branch 'chore/dependabot-rollout-2026-05-28-backend' set up to track 'origin/...'`。

- [ ] **Step 3: Create PR via gh**

```bash
cd ~/Desktop/ivy-backend
gh pr create --title "chore: 啟用 Dependabot 監控依賴（patch auto-merge）" --body "$(cat <<'EOF'
## Summary
- 新增 `.github/dependabot.yml`：每週一 09:00 Asia/Taipei 掃描 pip + github-actions ecosystem；4 個 group 合併常一起升的 dep；ignore 4 條 Python 3.9 cap + fastapi 0.136.3 supply-chain 版本
- 新增 `.github/workflows/dependabot-auto-merge.yml`：只 auto-merge `update-type == 'version-update:semver-patch'`，minor/major 必手審
- 修改 `requirements.txt`：4 條 dep 補上限 cap（python-multipart / Pillow / python-dotenv / requests），與 dependabot.yml ignore 對齊雙保險

## Prerequisites（合 PR 後 user 手動操作 GitHub UI）
1. Settings → Code security and analysis → Dependabot version updates: Enable
2. Settings → Actions → General → Workflow permissions: 勾 "Read and write permissions" + "Allow GitHub Actions to create and approve pull requests"
3. Settings → Branches → Branch protection rules → main: 勾 "Require status checks to pass before merging" + 加 required checks（依賴 CVE 掃描、Ruff DTZ Lint Gate、Tests (TZ=Asia/Taipei)、OpenAPI Drift Check、Centralized Settings Gate、Money Rounding Gate、Alembic Single Head Gate、Alembic Roundtrip、Alembic Symmetry Lint、Notification Dispatch Gate）

完整 prerequisite 與驗收見 `docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md` §7-§8。

## Test plan
- [ ] CI 全綠（pip-audit、ruff、tests、alembic gates、OpenAPI drift 等）
- [ ] PR 合入後到 Settings → Code security → Dependabot 看到 "Updates checked"
- [ ] 手動觸發 "Check for updates"，1-2 分鐘內可見 Dependabot PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: 印出 PR URL `https://github.com/.../pull/NN`。記下 PR 編號。

- [ ] **Step 4: 等 CI 全綠（user 可選擇 wait 或 fire-and-forget）**

```bash
gh pr checks <PR_NUM> --watch
```

或不等 CI，繼續 Task 5 frontend。CI 失敗的處理：到 Actions tab 看具體 failing job，多半是 unrelated test flake（本 PR 只動 config + 4 行 requirements.txt），若 pip-audit 因新 cap 抓到 CVE 為意外失敗則需 review。

---

## Task 5: Frontend `.github/dependabot.yml`

**Files:**
- Create: `ivy-frontend/.github/dependabot.yml`
- Branch: `chore/dependabot-rollout-2026-05-28-frontend`

- [ ] **Step 1: Switch to ivy-frontend repo & create branch**

```bash
cd ~/Desktop/ivy-frontend
git fetch origin
git checkout -b chore/dependabot-rollout-2026-05-28-frontend origin/main
```

Expected: 切到 frontend repo + 新 branch。

- [ ] **Step 2: Check frontend working tree status**

```bash
cd ~/Desktop/ivy-frontend
git status --short
```

紀錄 user 已有 WIP 的檔（不要動到）。後續所有 commit 只 `git add` 本 plan 列具體檔案。

- [ ] **Step 3: Verify .github/ exists**

```bash
ls ~/Desktop/ivy-frontend/.github/
```

Expected: 看到 `workflows/`。

- [ ] **Step 4: Create `dependabot.yml`**

寫入 `~/Desktop/ivy-frontend/.github/dependabot.yml`：

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
    labels:
      - "dependencies"
      - "frontend"
    commit-message:
      prefix: "chore(deps)"
      include: "scope"
    groups:
      fullcalendar:
        patterns:
          - "@fullcalendar/*"
      sentry:
        patterns:
          - "@sentry/*"
      vue-core:
        patterns:
          - "vue"
          - "vue-router"
          - "pinia"
      vitest-suite:
        patterns:
          - "vitest"
          - "@vitest/*"
          - "@vue/test-utils"
      typescript-tooling:
        patterns:
          - "typescript"
          - "vue-tsc"
          - "@vue/tsconfig"
          - "@types/*"

  # GitHub Actions third-party action 監控
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "monthly"
    labels:
      - "dependencies"
      - "ci"
    commit-message:
      prefix: "chore(ci)"
```

- [ ] **Step 5: Validate YAML syntax**

```bash
cd ~/Desktop/ivy-frontend
python3 -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add .github/dependabot.yml
git status --short
```

Expected: `A  .github/dependabot.yml` 屬本次；user WIP 其他檔仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
chore(ci): 加入 Dependabot 監控 npm + github-actions

每週一 09:00 Asia/Taipei 掃描；5 個 group 合併常一起升的 dep
（fullcalendar / sentry / vue-core / vitest-suite / typescript-tooling）。

Refs: ../ivy-backend/docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend Auto-merge Workflow

**Files:**
- Create: `ivy-frontend/.github/workflows/dependabot-auto-merge.yml`
- Branch: 同 Task 5

- [ ] **Step 1: Confirm on correct branch**

```bash
cd ~/Desktop/ivy-frontend
git branch --show-current
```

Expected: `chore/dependabot-rollout-2026-05-28-frontend`

- [ ] **Step 2: Create workflow file**

寫入 `~/Desktop/ivy-frontend/.github/workflows/dependabot-auto-merge.yml`：

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

⚠️ **安全約束**：本 workflow **不可**新增 `actions/checkout` 拉 PR head SHA + 跑 PR 內 untrusted code（如 `npm test`），否則攻擊者可送 malicious PR 在 main context 偷 secrets。本 workflow 只用 `dependabot/fetch-metadata` 讀 PR metadata + `gh pr merge` 觸發合併，無拉 PR code，安全。

- [ ] **Step 3: Validate YAML**

```bash
cd ~/Desktop/ivy-frontend
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/dependabot-auto-merge.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add .github/workflows/dependabot-auto-merge.yml
git status --short
```

Expected: `A  .github/workflows/dependabot-auto-merge.yml` 屬本次。

```bash
git commit -m "$(cat <<'EOF'
chore(ci): Dependabot patch PR auto-merge workflow

只 auto-merge patch update（`update-type == 'version-update:semver-patch'`），
minor/major 需手審。依賴 GitHub branch protection required checks 全綠
才會真合。

Refs: ../ivy-backend/docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Push Frontend Branch & Open PR

**Files:** 無新增；操作 GitHub 遠端

- [ ] **Step 1: Confirm 2 commits on branch**

```bash
cd ~/Desktop/ivy-frontend
git log --oneline origin/main..HEAD
```

Expected: 2 行 commit（dependabot.yml + workflow）。

- [ ] **Step 2: Push branch**

```bash
cd ~/Desktop/ivy-frontend
git push -u origin chore/dependabot-rollout-2026-05-28-frontend
```

- [ ] **Step 3: Create PR via gh**

```bash
cd ~/Desktop/ivy-frontend
gh pr create --title "chore: 啟用 Dependabot 監控依賴（patch auto-merge）" --body "$(cat <<'EOF'
## Summary
- 新增 `.github/dependabot.yml`：每週一 09:00 Asia/Taipei 掃描 npm + github-actions ecosystem；5 個 group 合併常一起升的 dep（@fullcalendar/* / @sentry/* / vue-core / vitest-suite / typescript-tooling）
- 新增 `.github/workflows/dependabot-auto-merge.yml`：只 auto-merge `update-type == 'version-update:semver-patch'`，minor/major 必手審

## Prerequisites（合 PR 後 user 手動操作 GitHub UI）
1. Settings → Code security and analysis → Dependabot version updates: Enable
2. Settings → Actions → General → Workflow permissions: 勾 "Read and write permissions" + "Allow GitHub Actions to create and approve pull requests"
3. Settings → Branches → Branch protection rules → main: 勾 "Require status checks to pass before merging" + 加 required checks（依賴 CVE 掃描、Tests & Build、OpenAPI Drift Check）

完整 prerequisite 與驗收見 ivy-backend repo `docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md` §7-§8。

## Test plan
- [ ] CI 全綠（npm audit、Tests & Build、OpenAPI Drift Check）
- [ ] PR 合入後到 Settings → Code security → Dependabot 看到 "Updates checked"
- [ ] 手動觸發 "Check for updates"，1-2 分鐘內可見 Dependabot PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: 印出 PR URL。

- [ ] **Step 4: 等 CI 全綠**

```bash
gh pr checks <PR_NUM> --watch
```

---

## Task 8: 印出 User Prerequisite Checklist

**Files:** 無修改；總結提醒 user 完成 GitHub UI 操作

- [ ] **Step 1: 等兩 PR 合入後**

回報 user 兩 PR 已合，並印出以下 checklist 給 user 在 GitHub UI 操作（**Claude 無法代做**）：

```
=== Dependabot 啟用後 user 手動操作清單（兩 repo 各做一次）===

對 ivy-backend 與 ivy-frontend 各自重複以下 4 步：

[ ] 1. Settings → Code security and analysis：
    - "Dependabot alerts" → Enable
    - "Dependabot security updates" → Enable
    - "Dependabot version updates" → Enable

[ ] 2. Settings → Actions → General → Workflow permissions：
    - 勾 "Read and write permissions"
    - 勾 "Allow GitHub Actions to create and approve pull requests"
    - Save

[ ] 3. Settings → Branches → Branch protection rules → main → Edit（或 Add rule）：
    - 勾 "Require a pull request before merging"
    - 勾 "Require status checks to pass before merging"
    - 勾 "Require branches to be up to date before merging"（建議）
    - 在 "Status checks that are required" 加入：
      Backend 必要 checks:
        - 依賴 CVE 掃描（HIGH-1）
        - Ruff DTZ Lint Gate
        - Tests (TZ=Asia/Taipei)
        - OpenAPI Drift Check
        - Centralized Settings Gate
        - Money Rounding Gate (round_half_up)
        - Alembic Single Head Gate
        - Alembic Roundtrip
        - Alembic Symmetry Lint
        - Notification Dispatch Gate (forbid line_service.notify_*)
      Frontend 必要 checks:
        - 依賴 CVE 掃描（HIGH-1）
        - Tests & Build
        - OpenAPI Drift Check
    - Save changes

[ ] 4. 手動觸發第一輪 Dependabot 掃描驗證設定生效：
    - Settings → Code security and analysis → Dependabot → "Recent updates"
    - 點 "Check for updates" 按鈕
    - 1-2 分鐘內到 Pull requests tab 看是否有 `chore(deps):` 開頭 PR

如果 step 4 沒生 PR：
- 檢查 dependabot.yml 在 Insights → Dependency graph → Dependabot 是否有 yaml parse error
- 檢查 step 1 是否真 enable

完整驗收與 troubleshooting 見：
ivy-backend/docs/superpowers/specs/2026-05-28-dependabot-rollout-design.md §7-§8
```

---

## Self-Review

**1. Spec coverage：**
- §3 Backend dependabot.yml → Task 1 ✓
- §3.2 requirements.txt → Task 3 ✓
- §4 Frontend dependabot.yml → Task 5 ✓
- §5 Auto-merge workflow（兩 repo）→ Task 2 + Task 6 ✓
- §7 Prerequisites checklist → Task 8 ✓
- §8 驗收 → Task 4 step 4 + Task 7 step 4 + Task 8 step 1 ✓
- §9 Out of scope → 已標明，無 task ✓
- §10 風險與回退 → 不需 task（design doc 留作備查）✓

**2. Placeholder scan：**
- 無 "TBD" / "TODO" / "implement later"
- 所有 YAML 完整提供
- 所有 commit message HEREDOC 完整
- 所有 git command 含 expected output
- PR title/body 完整提供

**3. Type consistency：**
- 兩個 repo 的 `dependabot-auto-merge.yml` 結構完全相同（只內容對稱差異 None）✓
- `pull_request_target` / `version-update:semver-patch` / `--auto --squash` 用詞一致 ✓
- Branch 命名 `chore/dependabot-rollout-2026-05-28-{backend,frontend}` 一致 ✓
- Commit message prefix `chore(ci)` / `chore(deps)` 與 Conventional Commits + Dependabot 預設一致 ✓
