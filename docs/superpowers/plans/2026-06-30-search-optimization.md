# 搜尋功能優化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓所有搜尋端點支援多關鍵字 AND + 全半形空格容錯，快速跳轉類（全域 Ctrl+K、教師 palette）多一層相關性排序，並修掉 `employees.py` / `students.py` 搜尋裸 `f"%{search}%"` 沒跳脫的 escape bug。

**Architecture:** 在既有 `utils/search.py` 擴充 4 個純函式（`normalize_query` / `tokenize_query` / `build_search_filter` / `relevance_key`），各搜尋端點改呼叫它們，而非各自手寫 ILIKE。純函式無 DB 依賴、可獨立單測；端點層改造為機械式套用。

**Tech Stack:** FastAPI、SQLAlchemy、PostgreSQL（測試用 SQLite TestClient，本功能無 PG 專屬行為）、pytest。

## Global Constraints

- **不建索引、不導入全文檢索**（資料量百級，YAGNI；對齊 2026-06-29 健檢）。
- **不動列表頁排序/分頁**（`employees` / `students` 列表維持 name/id 排序 + offset 分頁）。相關性排序只套快速跳轉類。
- **不碰** `api/activity/public.py` 精確 match 查詢（已防時序攻擊）。
- **portal messages section 維持 thread 時間排序、不做 token 拆分**（EXISTS subquery 多 token 重構成本高、收益低；只做正規化）。
- **後端針對性 pytest 一律加 `-o addopts=""`** 關 coverage，否則撞 120s timeout（見記憶 feedback_be_pytest_targeted_override_addopts）。
- **`.py` 檔 Edit 後 PostToolUse hook 會自動跑 black**——勿因格式被自動改而困惑（見記憶 feedback_subagent_posttooluse_black_hook）。
- **共用 checkout 紀律**：main 上有平行 session 的未提交 WIP。每次 commit 只精確 `git add -- <本任務檔>`，**不** `git add -A`、**不** `branch -f` / `reset`，不得掃入平行 WIP。
- 繁體中文 commit message、Conventional Commits、一個 commit 一件事。
- pytest 工作目錄一律 `cd /Users/yilunwu/Desktop/ivy-backend`。

---

## File Structure

| 檔案 | 角色 | Task |
|------|------|------|
| `utils/search.py` | 新增 4 純函式（核心） | T1 |
| `tests/test_search_utils.py` | 純函式單測（新建） | T1 |
| `api/search.py` | 全域搜尋 8 section 套 helper + 相關性 | T2 |
| `api/portal/search.py` | 教師 palette 5 section 套 helper + 相關性（students/guardians） | T3 |
| `api/employees.py` | 列表搜尋修 escape + multi-token | T4 |
| `api/students.py` | 列表搜尋修 escape + multi-token（保留 parent_name） | T5 |
| `api/activity/registrations_pending.py` | 後台審核搜學生加 multi-token（已 escape） | T6 |

**依賴**：T1 為基礎，T2–T6 各自 consume T1，且彼此 file-disjoint（不同 api 檔 + 不同測試檔）→ T1 完成後 T2–T6 可並行。

---

## Task 1: 擴充 `utils/search.py` 純函式

**Files:**
- Modify: `utils/search.py`（在既有 `escape_like_pattern` / `LIKE_ESCAPE_CHAR` 後新增）
- Test: `tests/test_search_utils.py`（新建）

**Interfaces:**
- Consumes: 既有 `escape_like_pattern(keyword)`、`LIKE_ESCAPE_CHAR`。
- Produces（T2–T6 依賴這些簽名）：
  - `normalize_query(raw: Optional[str]) -> str`
  - `tokenize_query(raw: Optional[str]) -> list[str]`
  - `build_search_filter(tokens: Sequence[str], columns: Sequence[ColumnElement]) -> Optional[ColumnElement]`
  - `relevance_key(text: Optional[str], normalized_query: str) -> int`（0=完全符合 / 1=前綴 / 2=包含；越小越相關）

- [ ] **Step 1: 寫 failing 測試**

建立 `tests/test_search_utils.py`：

```python
"""utils/search.py 純函式單元測試。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Student
from utils.search import (  # noqa: E402
    build_search_filter,
    normalize_query,
    relevance_key,
    tokenize_query,
)


def test_normalize_collapses_and_trims_whitespace():
    # 全形空白 U+3000 → 半形 + 連續空白收斂 + 去頭尾
    assert normalize_query("  王　小明 ") == "王 小明"


def test_normalize_fullwidth_alnum_to_halfwidth():
    assert normalize_query("ＡＢＣ１２３") == "ABC123"


def test_normalize_keeps_cjk_and_handles_none():
    assert normalize_query("王小明") == "王小明"
    assert normalize_query(None) == ""


def test_tokenize_splits_on_space_including_fullwidth():
    assert tokenize_query("大班 王") == ["大班", "王"]
    assert tokenize_query("王　小明") == ["王", "小明"]


def test_tokenize_blank_returns_empty():
    assert tokenize_query("   ") == []
    assert tokenize_query("") == []


def test_build_search_filter_none_when_empty():
    assert build_search_filter([], [Student.name]) is None
    assert build_search_filter(["王"], []) is None


def test_build_search_filter_escapes_wildcards():
    clause = build_search_filter(["%"], [Student.name])
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "\\%" in compiled  # % 被跳脫為 \%，不會當萬用字元


def test_build_search_filter_and_across_tokens():
    clause = build_search_filter(["大班", "王"], [Student.name])
    compiled = str(clause.compile()).upper()
    assert compiled.count(" LIKE ") == 2  # 兩 token 各一 LIKE
    assert " AND " in compiled  # token 之間 AND


def test_relevance_key_exact_prefix_contains():
    assert relevance_key("王", "王") == 0
    assert relevance_key("王小明", "王") == 1
    assert relevance_key("小王", "王") == 2


def test_relevance_key_casefold_and_empty():
    assert relevance_key("ABC", "abc") == 0  # 大小寫不敏感
    assert relevance_key("王", "") == 2  # 空 query 一律視為包含級
    assert relevance_key(None, "王") == 2
```

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_search_utils.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'normalize_query'` 或 collection error）

- [ ] **Step 3: 實作**

在 `utils/search.py` 開頭加 import，並在檔末（`LIKE_ESCAPE_CHAR = "\\"` 之後）新增函式：

```python
# 檔案開頭（在現有 docstring 之後）新增：
from __future__ import annotations

import unicodedata
from typing import Optional, Sequence

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement
```

```python
# 檔末新增（escape_like_pattern / LIKE_ESCAPE_CHAR 保持不動）：


def normalize_query(raw: Optional[str]) -> str:
    """正規化搜尋字串：全形→半形（NFKC，CJK 不受影響）、收斂空白。

    - NFKC 把全形英數/標點/全形空白（U+3000）轉半形；中日韓統一表意文字不變。
    - 連續空白（含 tab、全形空白轉成的半形空白）收斂為單一半形空白並去頭尾。
    """
    if not isinstance(raw, str):
        return ""
    s = unicodedata.normalize("NFKC", raw)
    return " ".join(s.split())


def tokenize_query(raw: Optional[str]) -> list[str]:
    """正規化後按空白分詞，去除空 token。"""
    normalized = normalize_query(raw)
    return normalized.split(" ") if normalized else []


def build_search_filter(
    tokens: Sequence[str], columns: Sequence[ColumnElement]
) -> Optional[ColumnElement]:
    """組多關鍵字 ILIKE 過濾：每 token 對 columns 做 OR，token 之間 AND。

    - 每個 token 經 escape_like_pattern 跳脫 % / _，防萬用字元注入。
    - tokens 或 columns 為空 → 回 None（呼叫端據此不加 filter）。
    """
    if not tokens or not columns:
        return None
    per_token_clauses = []
    for tok in tokens:
        pat = f"%{escape_like_pattern(tok)}%"
        per_token_clauses.append(
            or_(*[col.ilike(pat, escape=LIKE_ESCAPE_CHAR) for col in columns])
        )
    return and_(*per_token_clauses)


def relevance_key(text: Optional[str], normalized_query: str) -> int:
    """相關性排序鍵（越小越相關）：0=完全符合、1=前綴、2=包含/其他。

    比對採 casefold（與 ILIKE 大小寫不敏感一致）；text 先 normalize 對齊。
    normalized_query 須為已 normalize_query 過的字串。
    """
    if not normalized_query:
        return 2
    t = normalize_query(text).casefold()
    nq = normalized_query.casefold()
    if t == nq:
        return 0
    if t.startswith(nq):
        return 1
    return 2
```

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_search_utils.py -o addopts="" -q`
Expected: PASS（11 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- utils/search.py tests/test_search_utils.py
git commit -m "feat(search): utils/search 新增查詢正規化/分詞/多關鍵字過濾/相關性排序純函式" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- utils/search.py tests/test_search_utils.py
```

---

## Task 2: `api/search.py` 全域搜尋套 helper + 相關性排序

**Files:**
- Modify: `api/search.py`（8 個 `_search_*` section + `global_search` 端點）
- Test: `tests/test_search.py`（既有，append）

**Interfaces:**
- Consumes: T1 的 `normalize_query` / `tokenize_query` / `build_search_filter` / `relevance_key`。
- 改造規則：
  1. 每個 `_search_X(session, pattern, ...)` 簽名改為 `_search_X(session, tokens, nq, ...)`。
  2. 把 `or_(col.ilike(pattern, escape=...)...)` 換成 `build_search_filter(tokens, [cols])`（若回 None 則該 section 不會被呼叫——見端點守衛）。
  3. `.limit(SECTION_LIMIT)` 改 `.limit(SECTION_LIMIT * 3)`。
  4. dict 化後 `return _finalize(items, nq, "<primary_key>")`。
- 各 section 的 columns 與 primary_key：

  | section | columns | primary_key |
  |---------|---------|-------------|
  | students | `[Student.name, Student.student_id]` | `"name"` |
  | employees | `[Employee.name, Employee.employee_id]` | `"name"` |
  | guardians | `[Guardian.name, Guardian.phone]` | `"name"` |
  | classrooms | `[Classroom.name]` | `"name"` |
  | fees | `[StudentFeeRecord.student_name, StudentFeeRecord.fee_item_name]` | `"student_name"` |
  | activity | `[ActivityRegistration.student_name, ActivityRegistration.class_name]`（+`parent_phone` 若 `can_view_guardian_pii`） | `"student_name"` |
  | recruitment | `[RecruitmentVisit.child_name, RecruitmentVisit.address, RecruitmentVisit.notes, RecruitmentVisit.parent_response]` | `"child_name"` |
  | announcements | `[Announcement.title, Announcement.content]` | `"title"` |

- [ ] **Step 1: 寫 failing 測試**

在 `tests/test_search.py` 末尾 append（沿用該檔既有 `client_with_db` fixture 與建資料慣例；下列為要新增的測試意圖，實際 seed 請對齊檔內既有 helper）：

```python
def test_global_search_multi_token_and(client_with_db):
    """多關鍵字：『林 美』需 name 同時含『林』與『美』才命中。"""
    client, token = client_with_db  # 對齊既有 fixture 回傳；若不同請調整解包
    _seed_employee(client, name="林美麗", employee_id="T001")  # 對齊既有 seed helper
    _seed_employee(client, name="林大同", employee_id="T002")
    resp = client.get("/api/search", params={"q": "林 美"}, headers=_auth(token))
    names = [e["name"] for e in resp.json()["employees"]]
    assert "林美麗" in names
    assert "林大同" not in names


def test_global_search_relevance_order(client_with_db):
    """相關性：完全符合 < 前綴 < 包含。"""
    client, token = client_with_db
    _seed_employee(client, name="王", employee_id="T101")
    _seed_employee(client, name="王小明", employee_id="T102")
    _seed_employee(client, name="小王", employee_id="T103")
    resp = client.get("/api/search", params={"q": "王"}, headers=_auth(token))
    names = [e["name"] for e in resp.json()["employees"]]
    assert names.index("王") < names.index("王小明") < names.index("小王")


def test_global_search_wildcard_escaped(client_with_db):
    """escape 回歸：搜 '%' 不應命中全部（% 被跳脫為字面字元）。"""
    client, token = client_with_db
    _seed_employee(client, name="王小明", employee_id="T201")
    resp = client.get("/api/search", params={"q": "%%"}, headers=_auth(token))
    # '%%' 兩字元 ≥ MIN_QUERY_LEN，但跳脫後不 match 不含字面 '%' 的姓名
    assert resp.json()["employees"] == []
```

> 註：`_seed_employee` / `_auth` / fixture 解包請對齊 `tests/test_search.py` 既有寫法（該檔已 import `Employee`、`create_access_token`）。若既有檔用不同 seed 方式，沿用之，不要新造平行 fixture。

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_search.py -o addopts="" -q`
Expected: FAIL（多關鍵字未支援 → `test_global_search_multi_token_and` 失敗：`林大同` 也被『林』命中；相關性未排序 → order 斷言失敗）

- [ ] **Step 3: 實作**

在 `api/search.py`：

(a) import 換成：
```python
from utils.search import (
    LIKE_ESCAPE_CHAR,  # 若仍有他處用到則保留，否則移除
    build_search_filter,
    normalize_query,
    relevance_key,
    tokenize_query,
)
```
> 改造後各 section 不再直接用 `escape_like_pattern` / `LIKE_ESCAPE_CHAR`（改走 `build_search_filter`）。若 import 後有未用名稱，移除以免 flake8 F401。

(b) 在 section helpers 上方新增模組內 helper：
```python
def _finalize(items: list[dict], nq: str, key: str) -> list[dict]:
    """相關性排序（穩定排序保留 DB order_by 作 tie-break）後截斷。"""
    items.sort(key=lambda d: relevance_key(d.get(key), nq))
    return items[:SECTION_LIMIT]
```

(c) 逐一改 8 個 section helper。範例（students）：
```python
def _search_students(session, tokens, nq, current_user: dict) -> list[dict]:
    code = Permission.STUDENTS_READ.value
    unrestricted = is_row_unrestricted(current_user, code=code)
    qy = session.query(Student).filter(
        Student.is_active.is_(True),
        Student.lifecycle_status.notin_(_TERMINAL),
        build_search_filter(tokens, [Student.name, Student.student_id]),
    )
    if not unrestricted:
        scope = accessible_classroom_ids(session, current_user, code=code)
        if not scope:
            return []
        qy = qy.filter(Student.classroom_id.in_(scope))
    rows = qy.order_by(Student.name.asc()).limit(SECTION_LIMIT * 3).all()
    cr_map: dict[int, str] = {}
    cids = {r.classroom_id for r in rows if r.classroom_id}
    if cids:
        cr_map = {
            cid: name
            for cid, name in session.query(Classroom.id, Classroom.name)
            .filter(Classroom.id.in_(cids))
            .all()
        }
    items = [
        {
            "id": r.id,
            "name": r.name,
            "student_id": r.student_id,
            "classroom_name": cr_map.get(r.classroom_id, ""),
        }
        for r in rows
    ]
    return _finalize(items, nq, "name")
```
其餘 7 個 section 套相同三點（filter→`build_search_filter`、limit→`SECTION_LIMIT * 3`、return→`_finalize`），columns/primary_key 依本 Task「Interfaces」表。`_search_employees` 無 `current_user` 參數（簽名改 `(session, tokens, nq)`）；`_search_activity` 的 `search_cols` 改為：
```python
    cols = [ActivityRegistration.student_name, ActivityRegistration.class_name]
    if can_view_guardian_pii(current_user):
        cols.append(ActivityRegistration.parent_phone)
    rows = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.is_active.is_(True),
            build_search_filter(tokens, cols),
        )
        .order_by(ActivityRegistration.id.desc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [ ... 原 dict ... ]
    return _finalize(items, nq, "student_name")
```

(d) 端點 `global_search`：把 `pattern = ...` 段改為：
```python
    q_stripped = (q or "").strip()  # 保留供 audit summary
    nq = normalize_query(q)
    tokens = tokenize_query(q)
    if len(nq) < MIN_QUERY_LEN:
        return GlobalSearchResult(q=q)

    perms = current_user.get("permission_names")
```
並把每個 section 呼叫的 `pattern` 引數換成 `tokens, nq`，例如：
```python
        students = (
            _search_students(session, tokens, nq, current_user)
            if has_permission(perms, Permission.STUDENTS_READ)
            else []
        )
        employees = (
            _search_employees(session, tokens, nq)
            if has_permission(perms, Permission.EMPLOYEES_READ)
            else []
        )
        # ...guardians/classrooms/fees/activity/recruitment/announcements 同理：
        #   有 current_user 的（students/guardians/activity）傳 (session, tokens, nq, current_user)
        #   其餘傳 (session, tokens, nq)
```
audit 區塊不變（仍記 `q_stripped`）。

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_search.py tests/test_portal_search_audit_2026_05_14.py -o addopts="" -q`
Expected: PASS（新測試綠 + 既有 search 測試不退）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- api/search.py tests/test_search.py
git commit -m "feat(search): 全域搜尋支援多關鍵字與相關性排序" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- api/search.py tests/test_search.py
```

---

## Task 3: `api/portal/search.py` 教師 palette 套 helper + 相關性

**Files:**
- Modify: `api/portal/search.py`
- Test: `tests/test_portal_search.py`（既有，append）

**Interfaces:**
- Consumes: T1 函式。
- 改造：
  - 端點：`pattern = f"%{escape_like_pattern(q_stripped)}%"` → 新增 `nq = normalize_query(q)`、`tokens = tokenize_query(q)`；保留一個 `pattern = f"%{escape_like_pattern(nq)}%"`（**僅 messages section 用**，至少做到正規化）。`if len(q_stripped) < 2` 守衛改用 `if len(nq) < 2`。
  - **students**：`Student.name.ilike(pattern, ...)` → `build_search_filter(tokens, [Student.name])`；`.limit(SECTION_LIMIT)` → `.limit(SECTION_LIMIT * 3)`；在組出 `student_results`（含 parent_name 補值）後，依 `relevance_key(d["name"], nq)` 穩定排序並截 `SECTION_LIMIT`。
  - **guardians**：`or_(Guardian.name.ilike, Guardian.phone.ilike)` → `build_search_filter(tokens, [Guardian.name, Guardian.phone])`；limit*3；`guardian_results` 依 `relevance_key(d["name"], nq)` 排序截 `SECTION_LIMIT`。
  - **contact_book**：`or_(teacher_note.ilike, learning_highlight.ilike)` → `build_search_filter(tokens, [StudentContactBookEntry.teacher_note, StudentContactBookEntry.learning_highlight])`；**維持 `log_date.desc()` 排序與 `SECTION_LIMIT`**（不套相關性）。
  - **announcements**：`or_(title.ilike, content.ilike)` → `build_search_filter(tokens, [Announcement.title, Announcement.content])`；**維持 `created_at.desc()` 與 `SECTION_LIMIT`**（不套相關性）。
  - **messages**：**不改 token 拆分**，仍用單一 `pattern`（= 正規化後 nq 的 escaped pattern）；維持 EXISTS subquery 與 thread 時間排序。

- [ ] **Step 1: 寫 failing 測試**

在 `tests/test_portal_search.py` 末尾 append（對齊該檔既有 fixture 與 seed helper）：

```python
def test_portal_search_students_multi_token(portal_client):
    """『林 美』只命中 name 同時含兩 token 的學生。"""
    # 對齊既有 seed：建 name='林美麗' 與 '林大同' 的在籍學生於教師班級
    ...
    resp = ...get("/api/portal/search", params={"q": "林 美"})
    names = [s["name"] for s in resp.json()["students"]]
    assert "林美麗" in names and "林大同" not in names


def test_portal_search_students_relevance_order(portal_client):
    # 建 '王' / '王小明' / '小王'
    ...
    resp = ...get("/api/portal/search", params={"q": "王"})
    names = [s["name"] for s in resp.json()["students"]]
    assert names.index("王") < names.index("王小明") < names.index("小王")
```

> 註：`tests/test_portal_search.py` 已有完整 fixture/seed；沿用之填入上面 `...`，不要新建平行 app。

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_portal_search.py -o addopts="" -q`
Expected: FAIL（多 token 未支援 + 未排序）

- [ ] **Step 3: 實作**

依本 Task「Interfaces」逐 section 改。students section 末段排序範例：
```python
        student_results.sort(key=lambda d: relevance_key(d["name"], nq))
        student_results = student_results[:SECTION_LIMIT]
```
guardians section 同理對 `guardian_results`。import 加上 `build_search_filter, normalize_query, relevance_key, tokenize_query`（保留 `escape_like_pattern, LIKE_ESCAPE_CHAR` 給 messages 的 `pattern`）。

> 注意 students section 撈 `SECTION_LIMIT * 3` 後，後續「補 classroom_name」「補 parent_name（primary guardian）」的 in-clause 會作用在 3 倍候選上——這是必要的（排序前需完整 dict）；排序截斷後 dict 數回到 `SECTION_LIMIT`，回傳 payload 大小不變。

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_portal_search.py tests/test_portal_search_audit_2026_05_14.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- api/portal/search.py tests/test_portal_search.py
git commit -m "feat(search): 教師端 palette 學生/家長搜尋支援多關鍵字與相關性排序" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- api/portal/search.py tests/test_portal_search.py
```

---

## Task 4: `api/employees.py` 列表搜尋修 escape + multi-token

**Files:**
- Modify: `api/employees.py:286-288`
- Test: `tests/test_employees.py`（既有，append）

**Interfaces:**
- Consumes: T1 `tokenize_query` / `build_search_filter`。
- 列表頁**不套相關性排序**（維持既有分頁與 `offset/limit`）。

- [ ] **Step 1: 寫 failing 測試**

在 `tests/test_employees.py` append（對齊既有 fixture）：

```python
def test_employees_search_multi_token(...):
    # 建 name='林美麗','林大同'
    resp = ...get("/api/employees", params={"search": "林 美"})
    names = [e["name"] for e in resp.json()]
    assert "林美麗" in names and "林大同" not in names


def test_employees_search_wildcard_escaped(...):
    # 建 name='林美麗'（不含 '%'）
    resp = ...get("/api/employees", params={"search": "%"})
    # 修前：'%' 當萬用字元拉全部；修後：跳脫後 0 命中
    assert resp.json() == []
```

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_employees.py -o addopts="" -q`
Expected: FAIL（`test_employees_search_wildcard_escaped`：修前 `%` 拉全部 → 回非空 → 斷言失敗）

- [ ] **Step 3: 實作**

`api/employees.py` import 加：
```python
from utils.search import build_search_filter, tokenize_query
```
把 `get_employees` 內：
```python
        if search:
            like = f"%{search}%"
            q = q.filter(Employee.name.ilike(like) | Employee.employee_id.ilike(like))
```
改為：
```python
        tokens = tokenize_query(search)
        clause = build_search_filter(tokens, [Employee.name, Employee.employee_id])
        if clause is not None:
            q = q.filter(clause)
```
> `tokenize_query(None)` 回 `[]` → `clause is None` → 不過濾（列表頁不帶 search 時行為不變）。空字串/純空白同理 → 自然取代 min_length。

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_employees.py tests/test_export_employees_search.py tests/test_classrooms_employees_etag.py -o addopts="" -q`
Expected: PASS（含既有員工搜尋/匯出/etag 測試不退）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- api/employees.py tests/test_employees.py
git commit -m "fix(search): 員工列表搜尋跳脫萬用字元並支援多關鍵字" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- api/employees.py tests/test_employees.py
```

---

## Task 5: `api/students.py` 列表搜尋修 escape + multi-token（保留 parent_name）

**Files:**
- Modify: `api/students.py:540-546`
- Test: `tests/test_students_api.py`（既有，append）

**Interfaces:**
- Consumes: T1 `tokenize_query` / `build_search_filter`。
- 列表頁**不套相關性排序**；**保留** `parent_name` 比對欄位（C2 決策），只是改走 helper 順帶獲得跳脫。

- [ ] **Step 1: 寫 failing 測試**

在 `tests/test_students_api.py` append（對齊既有 fixture）：

```python
def test_students_search_multi_token(...):
    # 建 name='林美麗','林大同'
    resp = ...get("/api/students", params={"search": "林 美"})
    names = [s["name"] for s in resp.json()["items"]]
    assert "林美麗" in names and "林大同" not in names


def test_students_search_wildcard_escaped(...):
    # 建 name='林美麗'（不含 '%'）
    resp = ...get("/api/students", params={"search": "%"})
    assert resp.json()["items"] == []
```

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_students_api.py -o addopts="" -q`
Expected: FAIL（`%` 修前拉全部）

- [ ] **Step 3: 實作**

`api/students.py` import 加：
```python
from utils.search import build_search_filter, tokenize_query
```
把：
```python
        if search:
            like = f"%{search}%"
            q = q.filter(
                (Student.name.ilike(like))
                | (Student.student_id.ilike(like))
                | (Student.parent_name.ilike(like))
            )
```
改為：
```python
        tokens = tokenize_query(search)
        clause = build_search_filter(
            tokens, [Student.name, Student.student_id, Student.parent_name]
        )
        if clause is not None:
            q = q.filter(clause)
```

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_students_api.py tests/test_students_scope_lifecycle.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- api/students.py tests/test_students_api.py
git commit -m "fix(search): 學生列表搜尋跳脫萬用字元並支援多關鍵字" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- api/students.py tests/test_students_api.py
```

---

## Task 6: `api/activity/registrations_pending.py` 後台審核搜學生加 multi-token

**Files:**
- Modify: `api/activity/registrations_pending.py:232-243`
- Test: `tests/test_activity_admin_search_multitoken_2026_06_30.py`（新建；既有 PII gate 測試在 `test_activity_registration_search_guardian_pii_2026_06_23.py`，不動以免互擾）

**Interfaces:**
- Consumes: T1 `tokenize_query` / `build_search_filter`。
- 此端點**已有 escape + 完整 PII gate**，本 Task 只把單一 pattern 換成多關鍵字；**PII 條件式欄位（parent_phone / emergency_contact_phone）邏輯不變**——僅納入 `build_search_filter` 的 columns 清單。

- [ ] **Step 1: 寫 failing 測試**

建立 `tests/test_activity_admin_search_multitoken_2026_06_30.py`（fixture/seed 對齊 `test_activity_registration_search_guardian_pii_2026_06_23.py` 的建 app + admin token 慣例）：

```python
def test_admin_search_students_multi_token(...):
    # 建在籍學生 name='林美麗','林大同'
    resp = ...get("/api/students/search", params={"q": "林 美"})  # activity router
    names = [s["name"] for s in resp.json()["items"]]
    assert "林美麗" in names and "林大同" not in names
```

- [ ] **Step 2: Run test 驗證 fail**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_activity_admin_search_multitoken_2026_06_30.py -o addopts="" -q`
Expected: FAIL（單一 pattern 下『林 美』整串比對不到 → `林美麗` 不在結果）

- [ ] **Step 3: 實作**

`api/activity/registrations_pending.py` import 區（檔頭）加：
```python
from utils.search import build_search_filter, tokenize_query
```
把 `admin_search_students` 內：
```python
        # S2：跳脫 % / _ 萬用字元，避免搜尋 '%' 拉全校學生目錄
        like = f"%{escape_like_pattern(q.strip())}%"
        search_predicates = [
            Student.name.ilike(like, escape=LIKE_ESCAPE_CHAR),
            Student.student_id.ilike(like, escape=LIKE_ESCAPE_CHAR),
        ]
        if can_guardian:
            search_predicates.append(
                Student.parent_phone.ilike(like, escape=LIKE_ESCAPE_CHAR)
            )
            search_predicates.append(
                Student.emergency_contact_phone.ilike(like, escape=LIKE_ESCAPE_CHAR)
            )
        query = (
            session.query(Student, Classroom)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(
                Student.is_active.is_(True),
                or_(*search_predicates),
            )
        )
```
改為（保留 PII 欄位條件式，token 拆分 + 跳脫由 helper 負責）：
```python
        # S2：跳脫 % / _ 由 build_search_filter 負責；多關鍵字 token 間 AND
        search_cols = [Student.name, Student.student_id]
        if can_guardian:
            search_cols.append(Student.parent_phone)
            search_cols.append(Student.emergency_contact_phone)
        clause = build_search_filter(tokenize_query(q), search_cols)
        query = (
            session.query(Student, Classroom)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(Student.is_active.is_(True))
        )
        if clause is not None:
            query = query.filter(clause)
```
> `q` 經 `Query(..., min_length=1)`，但若為純空白 `tokenize_query` 回 `[]` → `clause is None` → 不加搜尋 filter（仍受 `is_active` 與下方 scope 過濾）。原行為對純 `%` 已跳脫；新行為一致且支援多 token。`escape_like_pattern` / `LIKE_ESCAPE_CHAR` / `or_` 若改造後無其他用處則移除未用 import。

- [ ] **Step 4: Run test 驗證 pass**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_activity_admin_search_multitoken_2026_06_30.py tests/test_activity_registration_search_guardian_pii_2026_06_23.py tests/test_pos_phone_search_guardian_guard_2026_06_22.py -o addopts="" -q`
Expected: PASS（新測試綠 + 既有 PII gate 測試不退）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add -- api/activity/registrations_pending.py tests/test_activity_admin_search_multitoken_2026_06_30.py
git commit -m "feat(search): 後台審核學生搜尋支援多關鍵字" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" -- api/activity/registrations_pending.py tests/test_activity_admin_search_multitoken_2026_06_30.py
```

---

## Task 7: 整合驗證（手動 smoke，無 code 改動）

**前端不需改動**（後端回傳順序改變即生效）。本 Task 為收尾驗證。

- [ ] **Step 1: 後端搜尋相關測試全跑一次**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest \
  tests/test_search_utils.py tests/test_search.py tests/test_portal_search.py \
  tests/test_portal_search_audit_2026_05_14.py tests/test_employees.py \
  tests/test_students_api.py tests/test_activity_admin_search_multitoken_2026_06_30.py \
  tests/test_activity_registration_search_guardian_pii_2026_06_23.py \
  -o addopts="" -q
```
Expected: 全 PASS。

- [ ] **Step 2: 前端既有搜尋元件測試不退**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-frontend && npm run test -- --run src/components/GlobalSearch tests 2>/dev/null || \
  echo "（對齊前端既有 vitest 指令，確認 GlobalSearch / PortalSearchPalette 測試綠）"
```
Expected: 既有前端測試綠（本任務未改前端，僅確認無連帶破壞）。

- [ ] **Step 3: 起 dev server 手動點一次（可選）**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh   # 後端 :8088 / 前端 :5173
```
登入後台（admin/ivytest123）→ Ctrl+K → 試「林 美」「王」「ＡＢＣ」（全形），確認多關鍵字命中、相關性順序、全形可搜。

- [ ] **Step 4: 收尾**

確認 6 筆 commit 皆在 `ivy-backend` main，無誤捲平行 WIP（`git log --oneline -6`、`git status --short` 應只剩平行 session 原有 WIP）。是否 push 觸發 Zeabur 部署由 user 決定。

---

## Self-Review（已完成，記錄供執行者參考）

- **Spec 覆蓋**：A（多關鍵字+容錯）→ T2–T6；B（相關性排序，只快速跳轉類）→ T2 全 section、T3 students/guardians；C1（escape bug）→ T4/T5；C2（保留 parent_name）→ T5；核心 helper → T1。✅ 全覆蓋。
- **Placeholder**：測試 seed 處標註「對齊既有 fixture」是刻意指引（既有測試檔已有 fixture，逐字複製反而易過時），非 TODO；helper 與端點改造 code 完整。
- **型別一致**：`normalize_query`/`tokenize_query`/`build_search_filter`/`relevance_key` 簽名在 T1 定義，T2–T6 呼叫一致；`_finalize` 為 search.py 模組內 helper（僅 T2）。
- **已知 trade-off**：相關性排序撈 `SECTION_LIMIT*3` 候選再 rank，極端情況（符合 >24 且完全符合排序在候選外）可能漏排最前——中文姓名場景幾乎不發生，spec 已記。
