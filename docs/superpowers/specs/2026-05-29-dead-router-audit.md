# 未掛載 router 盤點 — Sub-PR F

**Date**: 2026-05-29
**Status**: Audit script delivered；逐筆人工 review 為 follow-up
**Scope**: BE (ivy-backend)
**Spec**: 第五輪 P0 audit #22「30+ router 定義了但未掛載 main.py」

---

## 為何不直接刪 / 改

audit 點出「~30+ 個 router」但本 PR 改用 audit script 而**不自動刪除**，原因：

1. **誤判風險**：許多 router 透過 package `__init__.py` 鏈式 re-export（如 `api/salary/__init__.py` 聚合 `calculate.py / detail.py / ...`），主程式 import `from api.salary import router` 即可一次掛上。靜態掃描容易誤標為「dead」
2. **internal helper 模式**：部分 router 可能用於 internal job / scheduler tick 內部呼叫，而非 HTTP exposure
3. **lazy-import 慣例**：FastAPI 偶有 lazy import 模式（lifespan event 內 include）
4. **逐筆需業務決策**：archive (刪檔) vs 留 + 補 `# internal-only` docstring vs 重新接上 — 是業務+架構決策

故本 PR 只交付**可重複跑的盤點工具**，並列出 audit 明確點名的高優先 review 對象。

---

## 1. Audit script

`scripts/audit_unmounted_routers.py` — 掃 `api/` 下所有定義 `router = APIRouter(...)` 的檔案，並對照 `main.py` 與所有 `__init__.py` 的 import chain，列出**疑似 unmounted candidates**。

跑法：
```bash
cd ivy-backend
python3 scripts/audit_unmounted_routers.py
```

輸出 candidates list；**需逐筆人工 review**。

### 已知限制

- 只支援 `from api.X.Y import ...` absolute import（不解 `from .X import ...` relative）
- 不解 `importlib.import_module(...)` dynamic import
- 不檢查 `app.include_router(...)` 的「字面綁定」，假設 imported router 都會被 include

故 candidates 是 **suspicion list**，非確定 dead list。

---

## 2. Audit 明確點名高優先 candidates

依第五輪 P0 audit 文中明示：

| Module | 建議處置 |
|--------|--------|
| `api/leaves_quota.py` | review：保留或併入 `api/leaves.py`；若保留補 `# internal-only` docstring |
| `api/guardians_admin.py` | review：若家長端 LIFF 已不依賴，archive |
| `api/leaves_workday.py` | review：與 `api/leaves.py` 功能重疊？合併或刪 |

---

## 3. Follow-up 流程建議

1. 每月跑 `audit_unmounted_routers.py`，diff 上個月版本
2. 對新增 candidates，PR review 時必確認是否該補 include
3. 對長期上 candidates list 的 module，業務決策後 archive 到 `_archive/` 子目錄保留 git history
4. CI 可加 grep gate：任何 `APIRouter()` 必對應某處 include

---

## 4. 不做

- 不自動刪檔（誤刪風險）
- 不自動加 docstring（每個 module 需獨立判斷）
- 不接回 mount（須 PR review）

本 PR 純基建。

---

## 5. 參考

- `scripts/audit_unmounted_routers.py` 本 PR 加入
- 第五輪 P0 audit #22 原文
