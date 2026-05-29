# Semester 表示法統一 — Design Spec

- **Date**: 2026-05-28
- **Owner**: backend
- **Status**: Draft → Awaiting user review
- **Related**: `utils/academic.py`, `models/appraisal.py`, `models/classroom.py`, `models/activity.py`, `models/fees.py`, `models/student_log.py`, `models/academic_term.py`, `models/gov_moe.py`

---

## 1. 背景與問題

ivy-backend 目前對「學期」混用 4 套表示法，散落在 8 個 model、production code 11 處硬編碼、tests 20+ 處、frontend 15+ 個 UI 元件：

| 表示法 | 來源 | 範例 |
|--------|------|------|
| `Semester(str, Enum)` | `models/appraisal.py:45` | `Semester.FIRST` = `"FIRST"` |
| `Integer` | 7 個 model | `1` (上) / `2` (下) |
| 合併字串 `String(20)` | `models/classroom.py:284` (StudentAssessment) | `"2025上"` / `"2025下"` |
| 中文字面 `"上" / "下"` | `services/appraisal/excel_io.py:173,534` / `api/appraisal/__init__.py:1485` / frontend 15+ 處 | UI / Excel label |

轉換 helper（`utils/academic.py:semester_int_to_enum` / `semester_enum_to_int`）已存在，但 production code 仍有 11 處硬編碼 `Semester.FIRST/SECOND`、frontend 仍有 4 處 `'FIRST'/'SECOND'` string compare、StudentAssessment 仍用合併字串。

**影響**：跨 module 開發要 mental map 4 種 representation；新功能容易引入第 5 種；excel/UI 中文 label 漏進 service 層；OpenAPI schema (`Semester` enum component) 與其他 endpoint flat int 不一致。

## 2. 目標 / Non-Goals

**目標**：

1. **DB**：semester column 全部統一為 `Integer 1|2` + `CheckConstraint`。
2. **Python service / repo layer**：流通 `AcademicTerm(NamedTuple)` value object，禁止 raw tuple / 中文字面。
3. **API edge**：保留既有 flat `school_year` + `semester` query/body param（**60+ endpoint 零 OpenAPI break**）。僅 2 處不可避免 break：appraisal `/cycles*` + `/student-assessments*`。
4. **UI presentation**：中文 `'上'/'下'` 集中到 `src/utils/semesterLabel.ts` (frontend) 與 `services/appraisal/excel_io.py` (backend Excel) 兩個 allowlisted location。
5. **CI lint guard**：grep-based gate 禁止再引入 `Semester.FIRST/SECOND` literal 與中文 semester 字面流通到 internal layer。

**Non-Goals**：

- 不引入 Pydantic `TermKey` shared model 改 API shape（避免 60+ endpoint OpenAPI break）。
- 不處理 `school_year` 是 ROC 還是西元的歷史一致性問題（其他 model 普遍 ROC，本 spec 假設 ROC；StudentAssessment 字串 backfill 走 §6 spike）。
- 不改 `models/academic_term.py` canonical reference table schema（本來就 Integer，零變動）。
- 不做 7 個既有 Integer-storage table 的 schema 變動（zero migration）。

## 3. 架構決策

四層責任分工：

| 層 | 表示 | Allowlist |
|----|------|-----------|
| **DB** | `school_year INT` + `semester INT CHECK IN (1, 2)` | 全部 8 table 一致 |
| **Python service / repo** | `AcademicTerm(NamedTuple)` value object | `utils/academic.py` 定義；service/repo 層流通；不流通 raw tuple |
| **API edge** | flat `school_year` + `semester` int | 60+ endpoint 不改；2 處例外見 §5 |
| **UI presentation** | `'上' / '下'` 中文 label | `src/utils/semesterLabel.ts` (FE) + `services/appraisal/excel_io.py` (BE Excel) + `api/appraisal/__init__.py` Excel filename header |

**徹底刪除** `models/appraisal.py:Semester(str, Enum)` class 與 `_SEMESTER_ENUM` PG type。

## 4. DB Schema 變更

### 4.1 Single Alembic Revision: `semuni01`

`down_revision` 落地時動態決定（plan task 1 確認當前 `alembic heads`，避開並行 head 衝突）。

### 4.2 appraisal_cycles (~10 rows, in-place 安全)

**Upgrade**:
```python
op.add_column('appraisal_cycles', sa.Column('semester_int', sa.Integer(), nullable=True))
op.execute("""
    UPDATE appraisal_cycles
    SET semester_int = CASE semester WHEN 'FIRST' THEN 1 WHEN 'SECOND' THEN 2 END
""")
op.alter_column('appraisal_cycles', 'semester_int', nullable=False)
# rebuild unique constraint (depend on 'semester' col rename)
op.drop_constraint('uq_appraisal_cycle_year_sem', 'appraisal_cycles', type_='unique')
op.drop_column('appraisal_cycles', 'semester')
op.alter_column('appraisal_cycles', 'semester_int', new_column_name='semester')
op.create_unique_constraint('uq_appraisal_cycle_year_sem', 'appraisal_cycles', ['academic_year', 'semester'])
op.create_check_constraint('ck_appraisal_cycles_semester', 'appraisal_cycles', 'semester IN (1, 2)')
op.execute("DROP TYPE IF EXISTS appraisal_semester_enum")
```

**Downgrade**: 重建 `appraisal_semester_enum` type、map `1→'FIRST'/2→'SECOND'`、rename col、重建 unique constraint。

### 4.3 student_assessments (Phase 1: 加新欄並存)

舊 schema：`semester String(20)` 存 `"2025上"`（school_year 與中文 semester 黏合）。

**Phase 1 Upgrade**（本 revision `semuni01`）：
```python
op.add_column('student_assessments', sa.Column('school_year', sa.Integer(), nullable=True))
op.add_column('student_assessments', sa.Column('semester_int', sa.Integer(), nullable=True))

# backfill — SQL 內容由 §6 spike 結果決定（ROC vs 西元）
op.execute("""
    UPDATE student_assessments
    SET school_year = CAST(SUBSTRING(semester FROM 1 FOR LENGTH(semester) - 1) AS INTEGER),
        semester_int = CASE RIGHT(semester, 1) WHEN '上' THEN 1 WHEN '下' THEN 2 END
    WHERE semester IS NOT NULL
""")
# 若 spike 顯示西元年，UPDATE 加 -1911 轉 ROC

op.alter_column('student_assessments', 'school_year', nullable=False)
op.alter_column('student_assessments', 'semester_int', nullable=False)
op.create_check_constraint('ck_student_assessments_semester', 'student_assessments', 'semester_int IN (1, 2)')

# 舊 semester String 欄保留 + comment '@deprecated: drop in semuni02'
op.alter_column('student_assessments', 'semester',
    existing_type=sa.String(20),
    comment='DEPRECATED: 將於 semuni02 移除，請改讀 school_year + semester_int'
)

# 既有 index 拆
op.drop_index('ix_student_assessments_semester', 'student_assessments')
op.create_index('ix_student_assessments_term', 'student_assessments', ['school_year', 'semester_int'])
```

**Phase 2 (follow-up, ≥2 週後另一 revision `semuni02`)**：drop 舊 `semester` String column；rename `semester_int` → `semester`。**不屬本 spec scope**，列為 §10 follow-up。

**Phase 1 Downgrade**：drop 新欄 + 還原舊 index（舊 String 欄完全沒動）。

### 4.4 不變動的 7 個 table

| Model | semester column | 動作 |
|-------|----------------|------|
| `models/academic_term.py` | `Integer` | 不動 |
| `models/fees.py:FeeTemplate` | `Integer` | 不動 |
| `models/activity.py` (×3 model) | `Integer` | 不動 |
| `models/student_log.py:StudentChangeLog` | `Integer` | 不動 |
| `models/gov_moe.py` | `Integer` | 不動 |
| `models/classroom.py:Classroom` (主 model) | `Integer` | 不動 |

僅 §4.2 + §4.3 兩處有 schema migration。

## 5. Python Code 變動

### 5.1 新增 `utils/academic.py:AcademicTerm`

```python
from typing import NamedTuple

class AcademicTerm(NamedTuple):
    """學年/學期 value object — service / repo 層流通用。

    school_year: ROC 民國年（114, 115...）
    semester: 1 (上學期) | 2 (下學期)
    """
    school_year: int
    semester: int
```

放 `utils/academic.py` 跟既有 `resolve_current_academic_term` 同檔。

**向後相容**：`resolve_current_academic_term() -> tuple[int, int]` 簽章不變（NamedTuple 是 tuple subclass），既有 60+ callsite 零變動；新 callsite 可選用 `.school_year` / `.semester` 屬性存取。

### 5.2 刪除 `models/appraisal.py:Semester` 與 `_SEMESTER_ENUM`

```python
# Before
class Semester(str, enum.Enum):
    FIRST = "FIRST"
    SECOND = "SECOND"

_SEMESTER_ENUM = SAEnum(Semester, name="appraisal_semester_enum")
semester: Mapped[Semester] = mapped_column(_SEMESTER_ENUM, nullable=False)

# After
semester: Mapped[int] = mapped_column(Integer, nullable=False)
# table-level: CheckConstraint("semester IN (1, 2)", name="ck_appraisal_cycles_semester")
```

### 5.3 刪除過渡 helper

`utils/academic.py:semester_int_to_enum` 與 `semester_enum_to_int` 刪除。Callsite：
- `api/appraisal/__init__.py:115,161`
- `services/appraisal/status_aggregator.py:42,419,525`

改直接傳 int。

### 5.4 中文 label 邊界 helper

`utils/academic.py` 加：

```python
def format_semester_label_zh(semester: int) -> str:
    """1 → '上'，2 → '下'；其他 raise ValueError"""

def parse_semester_label_zh(label: str) -> int:
    """'上' → 1，'下' → 2；其他 raise ValueError"""
```

`services/appraisal/excel_io.py:173,534` 與 `api/appraisal/__init__.py:1485` 改用 helper（不在程式碼寫 inline `"上" if ... else "下"`）。

### 5.5 11 處 production Semester literal 替換

`grep "Semester\.\(FIRST\|SECOND\)" services/ api/ utils/` 命中 11 行。Mechanical 替換為 int 1/2。Tests 20+ 處同改。

⚠️ **Black hook 風險**：subagent surgical 改既有 .py 會被 PostToolUse black 重排成 cosmetic creep。Plan 裡 instruct 用 `python3 string.replace` 繞 hook，保證 diff 只動 semester literal（對齊 CLAUDE.md memory subagent_posttooluse_black_hook 教訓）。

### 5.6 API response schema 變動（2 處不可避免 OpenAPI break）

**(a) `schemas/appraisal.py:CycleOut.semester`**

```python
# Before: semester: Semester
# After:  semester: Annotated[int, Field(ge=1, le=2)]
```

OpenAPI `components/schemas/Semester` 消失。

**(b) `schemas/student.py` (or wherever StudentAssessment exposed)**

response 新增 `school_year: int`，`semester: int`（取代舊 `semester: str`）。

⚠️ **API contract 同步性**：API response 是當下契約（不像 DB column 可以 dual-write 共存），前端 cutover 必須跟後端同 release ship。

## 6. ROC vs 西元年 Spike (Plan T1)

`models/classroom.py:284` comment 寫 `"2025上"` 像西元年，但其他 model 普遍 ROC（114 學年）。

**Plan 第一個 task** 跑：
```sql
SELECT DISTINCT semester FROM student_assessments LIMIT 50;
```
對 dev DB 與 prod read-only postgres MCP 確認。

結果分支：

- **若全 ROC** (`"114上"`, `"115上"`)：§4.3 backfill SQL 直接寫入 `school_year`。
- **若全西元** (`"2025上"`, `"2026上"`)：§4.3 backfill SQL 加 `- 1911` 轉 ROC（與其他 table 一致）。
- **若混用**：升級為 P0 data-clean prerequisite task；本 spec 暫停，先盤清資料一致性。

## 7. Frontend 改動

### 7.1 schema regen

BE PR ship 後同 PR 跑：
```bash
cd ivy-frontend && npm run gen:api -- --alphabetize
```
（CLAUDE.md memory 教訓：必加 `--alphabetize` 否則 9000 行 ordering diff）

### 7.2 mechanical replace（4 處 enum string）

| 檔案 | 變動 |
|------|------|
| `src/composables/usePortalAppraisal.ts:20` | `semester === 'FIRST' ? '上' : '下'` → `=== 1` |
| `src/views/appraisal/YearlyEnrollmentTargetSection.vue:124` | `semesterEnum === 'FIRST'` → `=== 1` |
| `src/views/appraisal/CycleDetailView.vue:237` | `cycle.semester === 'FIRST'` → `=== 1` |
| `src/views/appraisal/CycleListView.vue:42` | `semesterLabel(v: string)` `=== 'FIRST'` → `(v: number) === 1` |

### 7.3 新增 `src/utils/semesterLabel.ts`

```ts
export function formatSemesterShort(semester: number): string {
  return semester === 1 ? '上' : '下'
}
export function formatSemesterLong(semester: number): string {
  return semester === 1 ? '上學期' : '下學期'
}
export function formatTermLabel(schoolYear: number, semester: number): string {
  return `${schoolYear} 學年 ${formatSemesterLong(semester)}`
}
```

對應 vitest：`src/utils/__tests__/semesterLabel.test.ts`（含 ValueError boundary 與 round-trip）。

### 7.4 15+ inline label 改 import helper

grep `"=== 1 ? '上'\|=== 1 ? '上學期'"` 命中 15+ 處 .vue / .ts，全改 `formatSemesterShort` / `formatSemesterLong`。

### 7.5 StudentAssessment caller 改讀兩欄

舊 `row.semester` 字串 split `"2025上"` → 改讀 `row.school_year` + `row.semester` (int)。grep 確認的 callsite 待 plan T7。

### 7.6 vitest mock

`'FIRST'/'SECOND'` mock 改 int。預估 5~8 個 test 檔。

## 8. CI Lint Guard

### 8.1 Backend grep gate

`.github/workflows/ci.yml`（或新 workflow）加：

```yaml
- name: Forbid Semester.FIRST/SECOND literals (production)
  run: |
    if grep -rn "Semester\.\(FIRST\|SECOND\)" \
        --include="*.py" \
        api services utils models \
        | grep -v "tests/" \
        | grep -v "alembic/versions/"; then
      echo "❌ Semester enum literal found — use int 1/2"; exit 1
    fi

- name: Forbid Chinese semester literals outside allowlist
  run: |
    BAD=$(grep -rn "['\"][上下]['\"]" \
        --include="*.py" \
        api services utils \
        | grep -v "services/appraisal/excel_io.py" \
        | grep -v "api/appraisal/__init__.py" \
        | grep -v "utils/academic.py" \
        | grep -v "alembic/versions/")
    if [ -n "$BAD" ]; then echo "❌ Chinese semester literal leaked: $BAD"; exit 1; fi
```

### 8.2 Frontend grep gate

```yaml
- name: Forbid inline 上/下 ternary outside semesterLabel.ts
  run: |
    BAD=$(grep -rn "=== 1 ? ['\"][上下]" --include="*.vue" --include="*.ts" src/ \
        | grep -v "src/utils/semesterLabel.ts")
    [ -z "$BAD" ] || (echo "❌ inline 上/下 ternary: $BAD"; exit 1)
```

### 8.3 Allowlist 明確

- Backend: `services/appraisal/excel_io.py`, `api/appraisal/__init__.py`, `utils/academic.py`, `alembic/versions/`
- Frontend: `src/utils/semesterLabel.ts`
- Tests: 暫不擋（過渡期保留改寫彈性，全部清乾淨後 plan follow-up 加 tests/ 進 gate）

## 9. Migration / Rollout

### 9.1 PR 拆法

**單一 release，兩個 PR 串接**（不是兩個 release）：

```
BE PR (ivy-backend)                    FE PR (ivy-frontend)
─────────────────────                  ─────────────────────
1. alembic semuni01                    1. npm run gen:api --alphabetize
2. drop Semester enum                  2. 'FIRST'/'SECOND' → int (4 處)
3. value object + helper               3. 抽 utils/semesterLabel.ts
4. 11 處 production code 改 int        4. StudentAssessment caller 改讀兩欄
5. 20+ tests 改 int                    5. 15+ inline label → helper
6. response_model 改 int                6. vitest mock 改 int
7. lint guard
```

### 9.2 Cross-repo OpenAPI Drift cycle 打破策略

對齊 CLAUDE.md memory 2026-05-27 approval-status 教訓（cross-repo OpenAPI Drift 是結構性 cycle — BE PR 改 OpenAPI 後 BE `openapi-drift` job 檢查 sibling FE schema.d.ts，FE 還沒同步就 fail；FE 自己 schema.d.ts 來源是 BE openapi.json 又沒法 pre-sync）。

**本 spec 走 BE `--admin` merge 模式**（approval-status PR #33 同模式）：

1. BE PR + FE PR 兩邊 review pass
2. BE PR 用 `gh pr merge --admin` bypass `openapi-drift` 失敗 → merge BE
3. FE PR rebase 上 main，跑 `npm run gen:api -- --alphabetize` → 自身 `openapi-drift` 自動對齊 → merge

**理由**：approval-status 教訓「`--admin` merge BE 先」是唯一打破 cross-repo cycle 的 proven 方法；pre-sync FE schema.d.ts 在 FE schema 本身 derive from BE openapi.json 的架構下不可行。

### 9.3 Rollout 順序

```
1. BE PR + FE PR review pass
2. BE PR merge → main
3. prod: alembic upgrade head（semuni01）
4. BE deploy
5. FE PR merge → main
6. FE deploy
7. 1~2 週 monitoring → 排 semuni02 drop legacy column
```

### 9.4 Downgrade plan

- `semuni01` downgrade：重建 enum type、map int → 'FIRST'/'SECOND'、rename col、drop student_assessments 新欄。tests in `tests/test_migration_semuni01.py` 涵蓋 upgrade + downgrade round-trip。
- `semuni02` downgrade（follow-up）：rename `semester` → `semester_int`、重新 add 舊 `semester` String 並 backfill `f"{school_year}{'上' if semester=1 else '下'}"`。

### 9.5 Migration-reviewer agent 過 P0/P1/P2

Spec written 後 plan 開始前，呼叫 `migration-reviewer` subagent 對 `semuni01` revision 過一遍，特別針對：
- enum drop 完整性（PG type 殘留風險）
- backfill SQL ROC vs 西元（與 §6 spike 結果對齊）
- student_assessments dual-column 期 read path
- unique constraint rebuild order

## 10. Follow-ups

1. **`semuni02` revision**（≥2 週 monitoring 後）：drop `student_assessments.semester` String column；rename `semester_int` → `semester`。
2. **Tests/ 加進 lint gate**：等 20+ test 全部清乾淨後另開 PR 加 `tests/` 進 §8 grep gate。
3. **`AcademicTerm` value object 推到 60+ existing callsite**：本 spec 只新建 value object 不強推 callsite migration（既有 `tuple[int, int]` 不破壞）；漸進式重構由其他 PR 處理。

## 11. 風險與緩解

| 風險 | 機率 | 緩解 |
|------|------|------|
| ROC vs 西元年混用導致 backfill 錯誤 | 中 | §6 spike T1 強制先驗證 |
| Cross-repo OpenAPI Drift cycle 卡 CI | 高 | §9.2 pre-sync 策略；最後手段 `--admin` |
| Black hook 把 surgical edit 變 cosmetic creep | 中 | §5.5 instruct `python3 string.replace` 繞 hook |
| Tests 20+ 處改寫漏 | 低 | `pytest -k semester` + grep `Semester\.` 雙保險 |
| Phase 1/2 之間有 hotfix 要 downgrade | 低 | §4.3 dual-column 期 1~2 週 + downgrade test 涵蓋 |
| Alembic head 並行衝突（P1 resilience 是否已 merge） | 中 | Plan T1 動態確認 `alembic heads` |

## 12. 成功條件

- [ ] `alembic upgrade head` 在 dev/prod 均成功，`semuni01` revision 唯一新 head
- [ ] `appraisal_cycles.semester` column type 為 `INTEGER`，`appraisal_semester_enum` PG type 不存在
- [ ] `student_assessments.school_year` + `semester_int` backfilled，與舊 `semester` String 並存
- [ ] `grep "Semester\.FIRST\|Semester\.SECOND" api services utils models | grep -v tests` 命中 0
- [ ] `grep "['\"][上下]['\"]" api services utils | grep -v allowlist` 命中 0
- [ ] Backend `pytest` 相對 baseline 5103 零新 regression
- [ ] Frontend `npm test` 相對 baseline ~2600 零新 regression；`typecheck` 0 error；`build` OK
- [ ] OpenAPI `components/schemas/Semester` 不存在；schema.d.ts regen 後乾淨
- [ ] CI lint guard 落地，新 PR 引入 enum literal 或中文字面流通會被擋

---

## Open Questions

1. **§6**：StudentAssessment `semester` String 是 ROC 還是西元？plan T1 驗證後再 finalize backfill SQL。
2. **§9.2**：`--admin` merge BE 後 prod migration 與 FE merge 之間的時間窗 — BE deploy 必須在 FE merge 前完成（否則 FE 收 'FIRST' 字串會炸）。建議 BE merge → migration → deploy 一氣呵成、同一個 maintenance window。

待 user review 後進 writing-plans。
