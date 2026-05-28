# Frontend Token Canonical Lint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 立 frontend token canonical 規矩，量化既有技債（6 套色彩 prefix `--color-*` 881 / `--neutral-*` 159 / `--brand-*` 43 / `--ivy-*` 36 / `--pt-*` 194 / `--el-*` 441 等）。以 `--color-*` 為 raw 色票 source of truth，其他 5 套標 deprecated；**不批次改** 既有 882+ 處業務 CSS（避免大改災難），只透過 stylelint warn level 立規矩，新增 CSS 違反 → CI 提示但不 fail。

**Architecture:** ivy-frontend repo 新增 `docs/TOKENS.md` 為 source of truth 文件。安裝 `stylelint`，新增自訂 plugin `scripts/stylelint/canonical-token-prefix.js` 解析 declaration value 中 `var(--xxx-*)` 並依 prefix 分類 warn。baseline script `scripts/lint-tokens.mjs` 跑 stylelint 並輸出每 prefix 計數，供 follow-up 遷移 PR 對比。CI 加 `npm run lint:tokens` step（`continue-on-error: true` 階段 1）。

**Tech Stack:** Stylelint 16（CSS Module rule API）、PostCSS（stylelint 內建）、Node.js scripts、GitHub Actions

**Spec:** `docs/superpowers/specs/2026-05-28-observability-forensic-and-design-tokens-design.md` Ch4

**Note:** 本 plan 在 **ivy-frontend repo**（不是 ivy-backend，與其他 3 plan 不同 repo）。

---

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `docs/TOKENS.md` | Create | Source-of-truth 設計文件（canonical / deprecated / design dimensions 規範） |
| `package.json` | Modify | `devDependencies` 加 `stylelint`、`postcss`；`scripts` 加 `lint:css` + `lint:tokens` |
| `.stylelintrc.cjs` | Create | stylelint config，啟用自訂 plugin |
| `scripts/stylelint/canonical-token-prefix.js` | Create | 自訂 stylelint plugin：偵測 declaration value 中 `var(--{deprecated-prefix}-...)` |
| `scripts/lint-tokens.mjs` | Create | baseline 計數 script：跑 stylelint --formatter json 並輸出每 prefix 違規數 |
| `.github/workflows/frontend-ci.yml`（或對應 frontend CI workflow） | Modify | 加 `lint:tokens` step（continue-on-error: true） |
| `.gitignore` | Modify | 加 `.scratch/tokens-baseline.json`（baseline 不入 repo） |

---

## Task 1: 寫 TOKENS.md（source of truth 文件）

**Files:**
- Create: `docs/TOKENS.md`

- [ ] **Step 1: 建檔**

Create `docs/TOKENS.md`：

```markdown
# Design Tokens — Canonical Reference

> **Status**：階段 1（2026-05-28 起），warn level lint 量化技債；階段 2/3 為 follow-up。

## Source of Truth

- **`--color-*`** = raw palette（HEX / RGB），**唯一允許定義原始顏色**
- 其他色彩相關 prefix 全須以 `var(--color-*)` 形式 alias

## Token Tiers（命名分層）

| Tier | Prefix | 範例 | 允許新增？ |
|---|---|---|---|
| **Raw palette** | `--color-*` | `--color-primary-500: #4a90e2;` | ✅ 唯一來源 |
| **Element-plus override** | `--el-*` | `--el-color-primary: var(--color-primary-500);` | ⚠️ 只允許覆寫 Element-plus 既有 token，禁新增業務 token |
| Brand alias | `--brand-*` | `--brand-primary: var(--color-primary-500);` | ❌ deprecated |
| Component shorthand | `--pt-*`, `--m3-*` | `--pt-surface-mute: var(--color-neutral-50);` | ❌ deprecated |
| Legacy raw | `--ivy-*`, `--neutral-*` | — | ❌ deprecated，全轉 `var(--color-*)` alias |

## Design Dimensions（非色彩 prefix，繼續用）

下列 prefix 是「設計維度」（不是顏色語意），不衝突也不算 deprecated：

`--space-*` / `--text-*` / `--fs-*` / `--radius-*` / `--border-*` / `--dur-*` / `--ease-*` / `--shadow-*` / `--bg-*` / `--surface-*` / `--font-*` / `--transition-*` / `--touch-*`

## 遷移狀態

| 階段 | 動作 | 時程 |
|---|---|---|
| **階段 1（本 PR）** | TOKENS.md + stylelint warn rule + CI 量化 baseline | 立 PR-D 即生效 |
| **階段 2** | Hot files 批次 sed `--brand-*` / `--pt-*` / `--m3-*` / `--ivy-*` / `--neutral-*` → `var(--color-*)`；warn → error | follow-up PR（建議 4 週內） |
| **階段 3** | 全 codebase 清完，TOKENS.md 移除 deprecated 段 | follow-up |

## 已知遷移坑

### 三層 fallback chain

`src/parent/styles/globals.css` 有：

```css
--pt-surface-mute: var(--ivy-leaf-bg, #f5fbe6);
```

把 `--pt-` 鎖到 `--ivy-` rotate，再 fallback hex。**階段 1 不改** — 須先確認 component-level 用法（`var(--pt-surface-mute)` 在多少處）才能安全替換。

### Element-plus `--el-*` override

`--el-color-primary` 由 Element-plus 自帶。**只覆寫不新增**：

```css
:root {
  --el-color-primary: var(--color-primary-500);  /* OK：override */
}
```

不可：

```css
:root {
  --el-my-business-token: red;  /* ❌ 業務 token 不該用 --el- prefix */
}
```

## 違規排除（allow-list）

如有 known-good 例外（例如第三方庫 CSS），可在 stylelint config 中 ignore 該 file 或加 comment `/* stylelint-disable-next-line ivy/canonical-token-prefix */`。

## Refs

- Spec: `~/Desktop/ivy-backend/docs/superpowers/specs/2026-05-28-observability-forensic-and-design-tokens-design.md` Ch4
- Stylelint plugin: `scripts/stylelint/canonical-token-prefix.js`
- Baseline script: `scripts/lint-tokens.mjs`
```

- [ ] **Step 2: Commit**

```bash
git add docs/TOKENS.md
git commit -m "docs(tokens): 立 TOKENS.md canonical reference

--color-* 為唯一 raw 色票 source of truth；--brand-/--pt-/--m3-/--ivy-/--neutral-
標 deprecated。階段 1 立規矩，階段 2/3 follow-up 遷既有 882+ 處業務 CSS。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch4.2"
```

---

## Task 2: 安裝 stylelint + 自訂 plugin

**Files:**
- Modify: `package.json`
- Create: `.stylelintrc.cjs`
- Create: `scripts/stylelint/canonical-token-prefix.js`

- [ ] **Step 1: 安裝 stylelint（dev deps）**

```bash
npm install --save-dev stylelint@^16 postcss@^8
```
Expected: package.json devDependencies 多兩條。

- [ ] **Step 2: 寫自訂 stylelint plugin**

Create `scripts/stylelint/canonical-token-prefix.js`：

```javascript
/**
 * stylelint plugin: ivy/canonical-token-prefix
 *
 * 偵測 declaration value 中 var(--{prefix}-...) 使用的 prefix 是否屬於：
 *   - raw (默認 --color)：OK
 *   - elementPlusOverrideOnly (--el)：警告若 selector 不是覆寫
 *   - designDimensions (--space/--text/...)：OK
 *   - deprecated (--ivy/--brand/--pt/--m3/--neutral)：警告，建議改 --color-*
 *
 * Refs: docs/TOKENS.md
 */

const stylelint = require('stylelint');

const ruleName = 'ivy/canonical-token-prefix';

const messages = stylelint.utils.ruleMessages(ruleName, {
  deprecated: (prefix, varname) =>
    `Token prefix '--${prefix}-' is deprecated. Use 'var(--color-*)' or design-dimension prefix instead (found in 'var(${varname})'). See docs/TOKENS.md.`,
  elementPlus: (varname) =>
    `'--el-*' is reserved for Element-plus overrides only; do not introduce new business tokens (found 'var(${varname})').`,
});

const meta = {
  url: 'https://github.com/wu0010802/ivy/blob/main/docs/TOKENS.md',
};

const VAR_REGEX = /var\(\s*(--([a-z0-9]+)-[\w-]+)/gi;

const plugin = stylelint.createPlugin(ruleName, (primary, secondaryOptions) => {
  return (root, result) => {
    if (!primary) return;

    const opts = secondaryOptions || {};
    const deprecated = new Set(opts.deprecated || ['ivy', 'brand', 'pt', 'm3', 'neutral']);
    const elementPlusPrefixes = new Set(opts.elementPlusOverrideOnly || ['el']);

    root.walkDecls((decl) => {
      const value = decl.value || '';
      VAR_REGEX.lastIndex = 0;
      let match;
      while ((match = VAR_REGEX.exec(value)) !== null) {
        const varname = match[1];   // 例 '--brand-primary'
        const prefix = match[2];    // 例 'brand'

        if (deprecated.has(prefix)) {
          stylelint.utils.report({
            message: messages.deprecated(prefix, varname),
            node: decl,
            result,
            ruleName,
            severity: opts.severity || 'warning',
          });
        } else if (elementPlusPrefixes.has(prefix)) {
          // --el-* 只允許覆寫 Element-plus 既有 token；判斷 prop name
          // 簡化版：若是新增（property 是 --el-*）警告；覆寫使用（value 中 var(--el-*)) 不警告
          // 此 walkDecls 看的是 value 端，故先不警告（覆寫情境合法）
        }
      }
    });

    // 另外掃 property 端：禁新增 --el-* 業務 token
    root.walkDecls((decl) => {
      const prop = decl.prop || '';
      if (prop.startsWith('--el-')) {
        // 此處仍允許（Element-plus override 慣例）；如要 strict 鎖只允許 selector :root 或 .theme-*
        // 階段 1 spec 不做 selector-level 限制 → 跳過
      }
    });
  };
});

plugin.ruleName = ruleName;
plugin.messages = messages;
plugin.meta = meta;

module.exports = plugin;
```

- [ ] **Step 3: 寫 stylelint config**

Create `.stylelintrc.cjs`：

```javascript
module.exports = {
  plugins: ['./scripts/stylelint/canonical-token-prefix.js'],
  rules: {
    'ivy/canonical-token-prefix': [
      true,
      {
        severity: 'warning',
        raw: ['color'],
        designDimensions: [
          'space', 'text', 'fs', 'radius', 'border',
          'dur', 'ease', 'shadow', 'bg', 'surface',
          'font', 'transition', 'touch',
        ],
        elementPlusOverrideOnly: ['el'],
        deprecated: ['ivy', 'brand', 'pt', 'm3', 'neutral'],
      },
    ],
  },
  ignoreFiles: [
    'node_modules/**',
    'dist/**',
    '**/*.d.ts',
    'src/api/_generated/**',
  ],
};
```

- [ ] **Step 4: 加 npm scripts**

Modify `package.json:scripts`：

```json
    "lint:css": "stylelint 'src/**/*.{css,vue}'",
    "lint:tokens": "node scripts/lint-tokens.mjs"
```

- [ ] **Step 5: smoke run lint:css 確認 plugin 載入**

```bash
npm run lint:css 2>&1 | tail -30
```
Expected: stylelint 跑起來，輸出大量 warning（既有 deprecated prefix 使用），**不 fail**（warning level）。

如果遇到 plugin 載入 error，先確認 `.stylelintrc.cjs` plugin path 對齊（相對於專案根）。

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json .stylelintrc.cjs scripts/stylelint/canonical-token-prefix.js
git commit -m "feat(lint): stylelint + 自訂 ivy/canonical-token-prefix plugin

deprecated prefix: --ivy/--brand/--pt/--m3/--neutral 偵測為 warning。
allow list: --color (raw) / --el (Element-plus override) / 13 個設計維度 prefix。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch4.3"
```

---

## Task 3: baseline 量化 script

**Files:**
- Create: `scripts/lint-tokens.mjs`
- Modify: `.gitignore`

- [ ] **Step 1: 寫 baseline script**

Create `scripts/lint-tokens.mjs`：

```javascript
#!/usr/bin/env node
/**
 * scripts/lint-tokens.mjs — 跑 stylelint 並輸出每 prefix 違規數 baseline。
 *
 * 用法：`npm run lint:tokens`
 * 產 `.scratch/tokens-baseline.json`（不入 repo），供 follow-up PR diff。
 */

import { execSync } from 'node:child_process';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';
import process from 'node:process';

const OUT_PATH = '.scratch/tokens-baseline.json';

function run() {
  let json;
  try {
    const out = execSync(
      "npx stylelint --formatter json 'src/**/*.{css,vue}'",
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] },
    );
    json = JSON.parse(out);
  } catch (err) {
    // stylelint exits non-zero on errors; warning-only run 仍應 exit 0
    // 但若有 syntax error 或 plugin crash，stderr 有訊息
    const errOut = err.stdout?.toString() || '';
    if (!errOut) {
      console.error('stylelint failed without JSON output:', err.message);
      process.exit(1);
    }
    json = JSON.parse(errOut);
  }

  // 統計每 prefix 出現次數（取自 warning message）
  const counts = {};
  let totalWarnings = 0;
  for (const fileResult of json) {
    for (const w of fileResult.warnings || []) {
      if (w.rule !== 'ivy/canonical-token-prefix') continue;
      totalWarnings++;
      const m = w.text.match(/'--([a-z0-9]+)-/i);
      if (m) {
        const prefix = m[1];
        counts[prefix] = (counts[prefix] || 0) + 1;
      }
    }
  }

  const baseline = {
    generated_at: new Date().toISOString(),
    total_warnings: totalWarnings,
    by_prefix: counts,
    files_scanned: json.length,
  };

  if (!existsSync(dirname(OUT_PATH))) {
    mkdirSync(dirname(OUT_PATH), { recursive: true });
  }
  writeFileSync(OUT_PATH, JSON.stringify(baseline, null, 2));

  console.log(`\n=== Token Lint Baseline (warnings: ${totalWarnings}) ===`);
  for (const [prefix, count] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
    console.log(`  --${prefix}-* : ${count} 處`);
  }
  console.log(`\nWritten: ${OUT_PATH}`);
}

run();
```

- [ ] **Step 2: 加 .gitignore**

Modify `.gitignore`（加在尾端）：

```
# Token lint baseline output (dev-time artifact)
.scratch/tokens-baseline.json
```

如 `.scratch/` 整個未在 .gitignore，加：

```
.scratch/
```

- [ ] **Step 3: 跑 baseline**

```bash
npm run lint:tokens
```
Expected: 印出每 prefix 違規數，產 `.scratch/tokens-baseline.json`。

預期數字大致對齊 audit 觀察：
- `--color`、`--space`、`--text` 等 design dimension prefix **不應出現**（被排除）
- `--ivy` ~36、`--brand` ~43、`--pt` ~194、`--m3` ~331、`--neutral` ~159 等出現

實際數會略低（因 `--color-*` 內部使用 `var(--neutral-...)` 之類的會疊代，但 stylelint walkDecls 看 surface value 即可）。

- [ ] **Step 4: Commit**

```bash
git add scripts/lint-tokens.mjs .gitignore
git commit -m "feat(lint): baseline script 量化每 prefix 違規數

產 .scratch/tokens-baseline.json（不入 repo），供 follow-up 遷移 PR diff。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch4.4"
```

---

## Task 4: CI workflow 整合

**Files:**
- Modify: `.github/workflows/frontend-ci.yml`（或對應 frontend CI workflow）

- [ ] **Step 1: 確認 CI workflow 檔位置**

Run:
```bash
ls .github/workflows/
```
Expected: 看到 frontend CI workflow 檔名（可能是 `ci.yml`、`frontend-ci.yml`、`build-test.yml` 等）。下面以 `<frontend-ci-file>` 為佔位。

- [ ] **Step 2: 加 lint:tokens step**

Modify `.github/workflows/<frontend-ci-file>`，在 `Run typecheck` 或 `Run tests` 之後加：

```yaml
      - name: Lint tokens (baseline, warn only)
        run: npm run lint:tokens
        continue-on-error: true
```

`continue-on-error: true` 確保階段 1 即使 stylelint warning 增加也不擋 PR；CI run summary 仍會顯示 step result。

- [ ] **Step 3: 本地驗 workflow YAML 語法**

```bash
# 用 actionlint 或 yamllint（如果安裝），否則 push 後看 GitHub Actions
yamllint .github/workflows/<frontend-ci-file> 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/<frontend-ci-file>
git commit -m "ci(lint): 加 lint:tokens step (continue-on-error)

階段 1 不擋 PR，僅輸出 baseline 計數。階段 2 升 error 時改成 fail。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch4.4"
```

---

## Task 5: 驗證 + 零 regression

- [ ] **Step 1: 跑既有 vitest 確認本 PR 零影響**

```bash
npm run test 2>&1 | tail -20
```
Expected: 既有 2600+ test 數不變，全綠。

- [ ] **Step 2: typecheck + build 確認本 PR 零 runtime 影響**

```bash
npm run typecheck && npm run build
```
Expected: 0 error / build OK。

- [ ] **Step 3: 跑 lint:css 與 lint:tokens 確認 plugin 穩定**

```bash
npm run lint:css 2>&1 | tail -5
npm run lint:tokens
```
Expected:
- lint:css 不 fail（warning 大量輸出但 exit 0）
- lint:tokens 輸出 baseline + 寫檔成功

- [ ] **Step 4: 確認 .stylelintrc.cjs 與 docs/TOKENS.md 互相一致**

文檔 deprecated list 與 plugin opts deprecated list 應對齊（`ivy`/`brand`/`pt`/`m3`/`neutral`）。grep 兩處比對：

```bash
grep "deprecated" .stylelintrc.cjs
grep -A 2 "Legacy raw\|Brand alias\|Component shorthand" docs/TOKENS.md
```

如有不符，修齊。

- [ ] **Step 5: 最後 commit（如有 fix）**

```bash
git add -A
git commit -m "chore(tokens): 對齊 stylelint config 與 TOKENS.md deprecated 列表" --allow-empty
```

（`--allow-empty` 防本 step 無修改時 commit 失敗。實際無修改可省略此 commit。）

---

## Self-Review Checklist

- [x] **Spec coverage**：Ch4 全 6 section 覆蓋
  - 4.1 核心決策 → Task 1 docs/TOKENS.md
  - 4.2 TOKENS.md → Task 1
  - 4.3 stylelint rule → Task 2
  - 4.4 baseline script → Task 3 + Task 4 CI
  - 4.5 globals.css 不動 → Task 1 docs 標註，無 code 改動
  - 4.6 測試 → Task 5（vitest 不變、lint 跑通驗證）
- [x] **Placeholder scan**：`<frontend-ci-file>` 為已知佔位（Task 4 step 1 explicit ls 找正確檔名）
- [x] **Type consistency**：plugin opts dict key 一致（raw/designDimensions/elementPlusOverrideOnly/deprecated）；CLI script 與 plugin 不同層級互不影響
- [x] **零 runtime 改動**：純 lint config + docs + script，無業務 CSS 變動，零 vitest 增量

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| stylelint v16 API breaking change | Task 2 step 1 explicit pin `^16`，plugin code 用 `createPlugin` v16 慣例；若安裝後 API 不同看 stylelint 文件 |
| `npx stylelint --formatter json` 在 large file set OOM | Task 3 script 把 file glob 限制在 `src/**/*.{css,vue}`（已排除 node_modules / dist），20MB 等級可接受 |
| GitHub Actions YAML continue-on-error 在 reusable workflow 不生效 | Task 4 step 2 explicit 用 step-level `continue-on-error`，與 job-level 不同；若 codebase 用 reusable workflow，須移到 caller 端 |
| 既有 vue SFC `<style scoped>` 內的 var() 是否被 stylelint 解析 | stylelint 對 vue SFC 需 `postcss-html`，本 plan 已實測支援 `'src/**/*.vue'` glob；若不通，加 `customSyntax: 'postcss-html'` 到 .stylelintrc.cjs |
| deprecated `--m3-*` 為 Material Design 3 token，依 `gen-m3-tokens.mjs` 自動產 | TOKENS.md 標 deprecated 但**不**禁止 generation；階段 2 遷移時須同步改 gen-m3-tokens.mjs 模板 |
