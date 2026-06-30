# 搜尋功能優化設計（多關鍵字 + 輸入容錯 + 相關性排序 + 一致性修正）

- 日期：2026-06-30
- 範圍：後端為主（前端幾乎不需改動）
- 觸發：使用者要求「優化搜尋功能」，brainstorming 後選定「全面健檢由我判斷」→ 收斂為「多關鍵字+容錯 / 相關性排序 / 修一致性小瑕疵」三方向。

---

## 1. 背景與判斷依據

系統有多套搜尋，技術現況一致：SQLAlchemy `ILIKE '%關鍵字%'`（leading wildcard），`employees.name` / `students.name` / `guardians.name` 無索引。

**關鍵判斷（為何不做效能/索引）**：本系統為單一園所，dev DB 實測資料量為 students 359、employees 36、classrooms 21、guardians 數筆（prod 同量級，上千封頂）。此規模下 `ILIKE '%x%'` 全表掃描為亞毫秒級，建 `pg_trgm` GIN 索引在純效能上是 YAGNI，且 prod 需裝 extension + migration + 每次寫入維護索引，成本遠大於收益。此結論對齊 2026-06-29 系統效能健檢「DB 索引層健康、不是瓶頸」。

**故本設計不碰效能/索引，聚焦使用者實際有感的體驗與一致性。**

### 已核實的程式碼事實
- 已有共用模組 `utils/search.py`（`escape_like_pattern` / `LIKE_ESCAPE_CHAR`）→ 擴充它，不新建。
- `api/search.py`（全域 Ctrl+K，staff-only）：8 類 section helper `_search_*(session, pattern, current_user)`，`SECTION_LIMIT=8`、`MIN_QUERY_LEN=2`，已用 `escape_like_pattern`。
- `api/portal/search.py`（教師端 palette）：5 類 section，已用 escape，部分用 EXISTS subquery。
- `api/employees.py:286-288`：`if search: like = f"%{search}%"` — **裸 f-string，未跳脫**（真 bug：搜 `%`/`_` 被當 wildcard）。無 min_length。
- `api/students.py:540-546`：同樣裸 f-string 未跳脫；且比對 `Student.name | student_id | parent_name`，`parent_name` 為 deprecated denormalized 副本。
- `api/activity/registrations_pending.py:199-270`：search-students，已用 `escape_like_pattern`。
- `api/activity/public.py`（家長前台 query-registration）：精確 match、含防時序延遲 → **不碰**。

---

## 2. 非目標（明確不做）

- ❌ 不建 `pg_trgm` / GIN 索引、不導入全文檢索（tsvector）。
- ❌ 不改列表頁（`employees` / `students` 列表）既有排序與分頁；相關性排序只套快速跳轉類。
- ❌ 不動 `api/activity/public.py` 精確 match 查詢（已防時序攻擊，碰了會破壞）。
- ❌ 前端不做多關鍵字高亮（目前單一 query 高亮即可；列為可選 nice-to-have，預設不做）。

---

## 3. 核心：擴充 `utils/search.py`（純函式、可獨立單測）

新增 4 個純函式（無 DB 依賴）：

### 3.1 `normalize_query(raw: str) -> str`
- `strip()` 前後空白。
- 全形空白（U+3000）→ 半形空白。
- 全形英數/標點 → 半形（`unicodedata.normalize("NFKC", ...)`）。**中文不受影響**（NFKC 不動 CJK 統一表意文字）。
- 連續空白 collapse 為單一半形空白。

### 3.2 `tokenize_query(raw: str) -> list[str]`
- `normalize_query` 後按空白 split，去除空 token。
- 例：`"王　小明"` → `["王", "小明"]`；`"  "` → `[]`。

### 3.3 `build_search_filter(tokens, columns) -> ColumnElement | None`
- 簽名：`build_search_filter(tokens: list[str], columns: Sequence[ColumnElement]) -> Optional[ColumnElement]`。
- 每個 token：`or_(col.ilike(f"%{escape_like_pattern(token)}%", escape=LIKE_ESCAPE_CHAR) for col in columns)`。
- tokens 之間：`and_(...)`。
- 空 tokens → 回 `None`（呼叫端據此「不加 filter」）。
- 不負責跨表 EXISTS（呼叫端自行組；本期所有端點欄位皆同表，足夠）。

### 3.4 `relevance_key(text, normalized_query) -> int`
- `0` = `normalize(text) == normalized_query`（完全符合）。
- `1` = `normalize(text).startswith(normalized_query)`（前綴）。
- `2` = 其他（包含）。
- 多 token 時以「正規化後的完整 query 字串」對主要欄位（name/title）比對。
- 給快速跳轉類 Python 排序用；平手時 fallback 原本的 `name.asc()`。

---

## 4. A — 多關鍵字 + 輸入容錯（套全部 ILIKE 端點）

各端點改用 `tokenize_query` + `build_search_filter`：
- `api/search.py` 8 個 `_search_*`
- `api/portal/search.py` 各 section
- `api/employees.py` `get_employees`
- `api/students.py` 列表搜尋
- `api/activity/registrations_pending.py` search-students

**語義**：每個 token 必須出現在「至少一個搜尋欄位」，token 間 AND。
→ 「大班 王」= `(任一欄位含「大班」) AND (任一欄位含「王」)`，找到大班的王同學；「王　小明」全形空格也能分詞。

---

## 5. B — 相關性排序（只套 `search.py` + `portal/search.py`）

- 各 section 的 DB 查詢：`limit(SECTION_LIMIT)` → 放寬為 `limit(SECTION_LIMIT * 3)`（=24）撈候選。
- Python 層：用 `relevance_key(主要欄位, normalized_query)` 排序，平手按原排序欄位（`name.asc()` 等）。
- 截斷取前 `SECTION_LIMIT`（=8）。
- 列表頁（employees / students）**完全不動**。

**Trade-off（已知，spec 註明）**：撈 24 再 rank，極端情況（某類符合 >24 筆且「完全符合」的列排在前 24 之外）可能漏排到最前。中文姓名場景幾乎不發生（姓名多為 2-3 字，`name == 單一 query` 極罕見；前綴符合者會落在候選內）。24 對百級資料量無感。

---

## 6. C — 一致性修正

- **C1**：`employees.py` / `students.py` 搜尋改走 `tokenize_query` + `build_search_filter`，**順帶修掉裸 f-string 未跳脫的 escape bug**。`normalize_query` 後為空（空字串/純空白）→ 不加 filter → 列表頁仍能不帶 search 列全部（自然取代 min_length，比硬性 min_length 更正確）。
- **C2**（已與 user 確認）：`students.py` 的 `parent_name` 比對 **保留**，只加上 escape（走共用 helper 即自動跳脫）。**不**改 Guardian.name EXISTS（收益低、風險中：一學生可多 guardian、query 變複雜）。parent_name 為 denormalized 副本，搜得到即可。

---

## 7. 測試（TDD）

### 7.1 純函式單測（`tests/test_search_utils.py` 新檔）
- `normalize_query`：全形空白→半形、全形數字「１２３」→「123」、連續空白 collapse、中文不變。
- `tokenize_query`：多 token、全形空格分詞、全空白回 `[]`。
- `build_search_filter`：多 token AND、單 token、空 → None；escape（含 `%`/`_`）。
- `relevance_key`：完全符合 < 前綴 < 包含 的排序鍵正確。

### 7.2 端點層（沿用既有 test 檔 / 新增）
- 多關鍵字 AND：「大班 王」命中大班的王、不命中別班的王。
- **escape 回歸**（重點，重現 C1 bug）：`employees` / `students` 搜 `%` 或 `_` **不會**拉全表（修前 RED）。
- 相關性：`search.py` 結果中「完全符合」排在「包含」之前。
- 空 search：列表頁正常回全部（不報錯、不誤用 `%%`）。

### 7.3 前端
- 後端回傳順序改變即生效，前端**不需改動**。
- 確認 `GlobalSearch.vue` / `PortalSearchPalette.vue` 既有測試仍綠。

---

## 8. 收尾與風險

- 純後端改動 + 共用 helper 收斂；前後端 commit 若前端無改動則只後端一筆。
- 共用 checkout 有平行 session WIP：commit 只精確 add 本任務檔，不掃別人 WIP，不 branch -f / reset。
- 完成定義：push + CI 綠 + （無 worktree 則略）。push 後端 = Zeabur 正式部署，由 user 決定時機。
