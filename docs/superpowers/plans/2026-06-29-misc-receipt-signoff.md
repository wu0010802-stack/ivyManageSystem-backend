# 雜項收款簽收（misc_receipts）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一個收入側「雜項收款簽收」模組，鏡像現有「廠商付款簽收」(`vendor_payments`)，記錄學費/活動以外的雜項進帳（場地租金、捐款、補助款、二手義賣、退費回收等），復用金額+簽收+簽名+附件機制，並納入財報收入。

**Architecture:** 全新獨立模組 `misc_receipts`，後端為純 router（無服務注入），鏡像 `vendor_payments` 的 model/schema/router/前端頁面，於收入語義處調整：欄位改名（`payment_date→receipt_date`、`vendor_name→payer_name`、`invoice_number→receipt_number`）、新增 `category` 軟枚舉、財報接入**收入側**（非支出，非純鏡像）。廠商付款模組完全不動。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + PostgreSQL（後端）；Vue 3 `<script setup lang="ts">` + Element Plus + Vite（前端）。

## Global Constraints

- **語言**：一律繁體中文（程式註解、docstring、commit message、UI 文案）。
- **權限為字串集合**：後端 `Permission` 是 `str Enum`，**無 bit/BigInt**；新權限**不**加進 `LEGACY_PERMISSION_BITS`（已凍結僅 alembic 用）。
- **前端 TS-only**：新 SFC 一律 `<script setup lang="ts">`；禁 `: any`/`as any`，用 `: unknown` + narrow。api wrapper 用 `.ts`。
- **後端測試掛 `test_db_session` fixture**（否則打到 dev PG）；針對性 pytest 加 `-o addopts=""` 關 coverage 避免 120s timeout。
- **財報收入口徑 = 含 pending**（與付款側 `vendor_payments` 支出口徑對齊：無論 pending/signed 都計入）。
- **`amount` CHECK 為 `> 0`**（非 `>= 0`；付款側初版 `>= 0` 已被 `vpamt01` 收緊，新表直接用 `> 0`）。
- **`category` 為軟枚舉**：DB 存字串 + 應用層白名單校驗（仿 `payment_method`）；新增類別只改常數+前端標籤，不需 migration。
- **prod 部署順序**：`permission_definitions` seed migration 必須在前端拉新欄位前合併並 `alembic upgrade heads`，否則非 wildcard admin 403。
- **共用 checkout 紀律**：commit 用 pathspec 精確提交自己的檔；移動 ref 用 `git merge` 讀 live tip，勿 `branch -f` 配過時 SHA。**後端、前端各自獨立 worktree + feature 分支**。
- **前後端分開 commit**：後端一筆、前端一筆，訊息描述同一功能。

---

## Execution Setup（任務開始前）

1. **後端 worktree**：`git -C /Users/yilunwu/Desktop/ivy-backend worktree add .claude/worktrees/misc-receipt -b feat/misc-receipt-signoff <base>`
   - **⚠ base 必須含 alembic head `parcuplk01`**（新 migration 的 down_revision 指向它）。先 `git -C ../ivy-backend log --oneline origin/main | grep parcuplk` 確認；若 `origin/main` 落後不含，與 user 確認用哪個 ref 當基線（可能需從 local main，但注意會帶入平行 WIP）。
2. **把 spec 帶進 worktree**：spec `docs/superpowers/specs/2026-06-29-misc-receipt-signoff-design.md` 目前在後端 main 工作樹未 commit。在 worktree 裡 `cp` 一份進來，作為 feature 分支第一個 commit：
   ```bash
   git -C /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/misc-receipt add docs/superpowers/specs/2026-06-29-misc-receipt-signoff-design.md docs/superpowers/plans/2026-06-29-misc-receipt-signoff.md
   git -C /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/misc-receipt commit -m "docs(misc-receipts): 雜項收款簽收設計與實作計畫"
   ```
3. **前端 worktree**（後端 router 完成後再開，見 Task 9）：`git -C /Users/yilunwu/Desktop/ivy-frontend worktree add .claude/worktrees/misc-receipt -b feat/misc-receipt-signoff <base>`，並 `ln -s ../../../node_modules node_modules`（前端 worktree node_modules symlink，見 workspace 記憶）。
4. 後續所有後端路徑以 worktree 根 `<BE>` = `/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/misc-receipt` 為準；前端 `<FE>` = `/Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/misc-receipt`。

---

## File Structure

**後端（`<BE>`）：**
- Create `models/misc_receipt.py` — MiscReceipt model（鏡像 VendorPayment + `category`）
- Modify `models/database.py` — re-export MiscReceipt（若該檔集中 import models）
- Create `alembic/versions/20260629_mscrcpt01_add_misc_receipts.py` — 建表
- Create `alembic/versions/20260629_mscrcptp01_seed_misc_receipt_perms.py` — 權限 seed
- Modify `utils/permissions.py` — enum / labels / SPLIT_MODULES / PERMISSION_GROUPS / ROLE_TEMPLATES
- Create `schemas/misc_receipts.py` — Out schemas
- Create `api/misc_receipts.py` — router（純 router，鏡像）
- Modify `main.py` — import + include_router
- Modify `services/finance_report_service.py` — 收入 provider + build_finance_summary / build_finance_detail 接入
- Create `tests/test_misc_receipts.py` — 端點/權限/簽收/終態/日期守衛
- Create `tests/test_misc_receipt_finance.py` — 財報收入聚合

**前端（`<FE>`）：**
- Create `src/api/miscReceipt.ts` — api wrapper（鏡像 vendorPayment.ts）
- Modify `src/constants/permissions.ts` — PERMISSION_NAMES + ROUTE_PERMISSION_RULES
- Modify `src/router/index.ts` — /misc-receipts route
- Modify `src/components/layout/AdminSidebar.vue` — 側邊欄入口
- Create `src/views/MiscReceiptView.vue` — 主頁面（鏡像 VendorPaymentView.vue）
- Create `src/components/MiscReceiptSignDialog.vue` — 簽收彈窗（鏡像）
- Modify `src/api/_generated/schema.d.ts` — OpenAPI codegen 產出
- Create `src/api/__tests__/miscReceipt.spec.ts` — api 封裝測試

---

## 識別碼替換對照表（vendor_payments → misc_receipts）

鏡像檔案時，對源檔做以下**精確全字串替換**，再套各 Task 列出的差異：

| 源（vendor_payments） | 目標（misc_receipts） |
|---|---|
| 表名 `vendor_payments` | `misc_receipts` |
| 類名 `VendorPayment` | `MiscReceipt` |
| 欄位 `payment_date` | `receipt_date` |
| 欄位 `vendor_name` | `payer_name` |
| 欄位 `invoice_number` | `receipt_number` |
| 權限 `VENDOR_PAYMENT_READ`/`VENDOR_PAYMENT_WRITE` | `MISC_RECEIPT_READ`/`MISC_RECEIPT_WRITE` |
| 路徑 `/vendor-payments` | `/misc-receipts` |
| tag `vendor-payments` | `misc-receipts` |
| 約束名前綴 `ck_vendor_payments_` | `ck_misc_receipts_` |
| 索引名 `ix_vendor_payments_` | `ix_misc_receipts_`（`_vendor_name`→`_payer_name`） |
| schema 類 `VendorPayment{Out,ListOut,SummaryOut,AttachmentMetaOut}` | `MiscReceipt{...}` |
| 前端檔 `vendorPayment.ts` | `miscReceipt.ts` |
| 前端函式 `*VendorPayment*` | `*MiscReceipt*` |
| 元件 `VendorPaymentView`/`VendorPaymentSignDialog` | `MiscReceiptView`/`MiscReceiptSignDialog` |
| 審計/UI 文案「廠商付款」 | 「雜項收款」 |
| 財報「廠商付款」支出 | 「雜項收款」**收入**（不同接入點，見 Task 7） |

**新增**（無對應源）：`category` 欄位、`RECEIPT_CATEGORIES` 常數、`ix_misc_receipts_category` 索引、`ck_misc_receipts_category` CHECK、前端 `CATEGORY_OPTIONS`。

---

## Task 1: MiscReceipt Model

**Files:**
- Create: `<BE>/models/misc_receipt.py`
- Modify: `<BE>/models/database.py`（若集中 import models，比照 `vendor_payment` 加一行 import）
- Test: `<BE>/tests/test_misc_receipts.py`（本 Task 只放 model 約束測試）

**Interfaces:**
- Produces: `models.misc_receipt.MiscReceipt`（ORM model，表 `misc_receipts`）；常數 `PAYMENT_METHODS`、`RECEIPT_STATUSES`、`SIGNATURE_KINDS`、`RECEIPT_CATEGORIES`。

- [ ] **Step 1: 寫失敗測試**（`<BE>/tests/test_misc_receipts.py`）

```python
import pytest
from datetime import date
from sqlalchemy.exc import IntegrityError
from models.misc_receipt import MiscReceipt, RECEIPT_CATEGORIES


def test_misc_receipt_amount_must_be_positive(test_db_session):
    row = MiscReceipt(
        receipt_date=date(2026, 6, 1), payer_name="某基金會", category="donation",
        amount=0, payment_method="cash", status="pending", attachments=[],
    )
    test_db_session.add(row)
    with pytest.raises(IntegrityError):
        test_db_session.flush()


def test_misc_receipt_categories_constant():
    assert set(RECEIPT_CATEGORIES) == {
        "rent", "donation", "subsidy", "secondhand_sale", "refund_recovery", "other"
    }
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: models.misc_receipt`）

- [ ] **Step 3: 建立 model**（`<BE>/models/misc_receipt.py`）

```python
"""
models/misc_receipt.py — 雜項收款簽收

園所對學費/活動以外雜項進帳（場地租金、捐款、補助款、二手義賣、退費回收等）
的紙本流數位化：登錄收款項目 → 收集繳款方簽收（簽名或照片）→ 留稽核痕跡。
與「廠商付款簽收」(vendor_payments) 鏡像對稱，方向相反（收入側）。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column, Integer, String, Text, Date, DateTime, ForeignKey, Index,
    Numeric, CheckConstraint, JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base

PAYMENT_METHODS = ("cash", "bank_transfer", "check", "linepay", "other")
RECEIPT_STATUSES = ("pending", "signed")
SIGNATURE_KINDS = ("drawn", "photo")
RECEIPT_CATEGORIES = (
    "rent", "donation", "subsidy", "secondhand_sale", "refund_recovery", "other",
)


class MiscReceipt(Base):
    __tablename__ = "misc_receipts"

    id = Column(Integer, primary_key=True)
    receipt_date = Column(Date, nullable=False, index=True)
    payer_name = Column(String(120), nullable=False, index=True)
    category = Column(String(20), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(20), nullable=False)
    description = Column(String(255))
    receipt_number = Column(String(60))
    notes = Column(Text)
    attachments = Column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list,
    )

    status = Column(String(16), nullable=False, default="pending")
    signer_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    signed_at = Column(DateTime)
    signature_kind = Column(String(16))  # drawn | photo | NULL
    signature_key = Column(String(255))

    created_by_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"))
    created_at = Column(DateTime, nullable=False, default=now_taipei_naive)
    updated_at = Column(
        DateTime, nullable=False, default=now_taipei_naive, onupdate=now_taipei_naive
    )

    signer = relationship("Employee", foreign_keys=[signer_id], lazy="joined")
    created_by = relationship("Employee", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_misc_receipts_amount_pos"),
        CheckConstraint(
            "payment_method IN ('cash','bank_transfer','check','linepay','other')",
            name="ck_misc_receipts_method",
        ),
        CheckConstraint(
            "status IN ('pending','signed')", name="ck_misc_receipts_status"
        ),
        CheckConstraint(
            "category IN ('rent','donation','subsidy','secondhand_sale','refund_recovery','other')",
            name="ck_misc_receipts_category",
        ),
        CheckConstraint(
            "signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
            name="ck_misc_receipts_signature_kind",
        ),
        Index("ix_misc_receipts_status_date", "status", "receipt_date"),
    )
```

- [ ] **Step 4: re-export**（若 `<BE>/models/database.py` 有集中 import 各 model）

確認 `vendor_payment` 在 `models/database.py` 怎麼被 import（grep `vendor_payment`），比照加一行 `from models.misc_receipt import MiscReceipt  # noqa: F401`。若無集中 import 機制（model 由各自 router import）則跳過此步。

- [ ] **Step 5: 跑測試確認 pass**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py -o addopts="" -q`
Expected: PASS（2 passed）

- [ ] **Step 6: commit**

```bash
cd <BE>
git add models/misc_receipt.py tests/test_misc_receipts.py
# 若改了 database.py：git add models/database.py
git commit models/misc_receipt.py tests/test_misc_receipts.py -m "feat(misc-receipts): 新增雜項收款 MiscReceipt model"
```

---

## Task 2: 建表 Migration

**Files:**
- Create: `<BE>/alembic/versions/20260629_mscrcpt01_add_misc_receipts.py`

**Interfaces:**
- Consumes: alembic head `parcuplk01`。
- Produces: revision `mscrcpt01`（表 `misc_receipts`）。

- [ ] **Step 1: 確認當前 head**

Run: `cd <BE> && python -m alembic heads`
Expected: 顯示 `parcuplk01 (head)`（single head）。若不同，以實際輸出當 down_revision。

- [ ] **Step 2: 寫 migration**（`<BE>/alembic/versions/20260629_mscrcpt01_add_misc_receipts.py`）

```python
"""misc_receipts: 雜項收款簽收（園務行政，收入側）

Revision ID: mscrcpt01
Revises: parcuplk01
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "mscrcpt01"
down_revision = "parcuplk01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "misc_receipts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("receipt_date", sa.Date, nullable=False),
        sa.Column("payer_name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("receipt_number", sa.String(60), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("signer_id", sa.Integer,
                  sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("signed_at", sa.DateTime, nullable=True),
        sa.Column("signature_kind", sa.String(16), nullable=True),
        sa.Column("signature_key", sa.String(255), nullable=True),
        sa.Column("created_by_id", sa.Integer,
                  sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_misc_receipts_amount_pos"),
        sa.CheckConstraint("payment_method IN ('cash','bank_transfer','check','linepay','other')",
                           name="ck_misc_receipts_method"),
        sa.CheckConstraint("status IN ('pending','signed')", name="ck_misc_receipts_status"),
        sa.CheckConstraint(
            "category IN ('rent','donation','subsidy','secondhand_sale','refund_recovery','other')",
            name="ck_misc_receipts_category"),
        sa.CheckConstraint("signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
                           name="ck_misc_receipts_signature_kind"),
    )
    op.create_index("ix_misc_receipts_receipt_date", "misc_receipts", ["receipt_date"])
    op.create_index("ix_misc_receipts_payer_name", "misc_receipts", ["payer_name"])
    op.create_index("ix_misc_receipts_category", "misc_receipts", ["category"])
    op.create_index("ix_misc_receipts_signer_id", "misc_receipts", ["signer_id"])
    op.create_index("ix_misc_receipts_status_date", "misc_receipts", ["status", "receipt_date"])


def downgrade() -> None:
    op.drop_index("ix_misc_receipts_status_date", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_signer_id", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_category", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_payer_name", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_receipt_date", table_name="misc_receipts")
    op.drop_table("misc_receipts")
```

- [ ] **Step 3: 驗證 single head + upgrade/downgrade roundtrip**

Run: `cd <BE> && python -m alembic heads && python -m alembic upgrade head && python -m alembic downgrade -1 && python -m alembic upgrade head`
Expected: head 仍 single（`mscrcpt01`）；upgrade 建表、downgrade 移除、再 upgrade 無錯。

> ⚠ 此操作會動到設定的 alembic DB。若指向 dev PG 且不想留痕，改用臨時 sqlite 或在測試 DB 上跑（依 repo alembic.ini 環境）。確認後再 commit。

- [ ] **Step 4: commit**

```bash
cd <BE>
git add alembic/versions/20260629_mscrcpt01_add_misc_receipts.py
git commit alembic/versions/20260629_mscrcpt01_add_misc_receipts.py -m "feat(misc-receipts): 建立 misc_receipts 表 migration"
```

---

## Task 3: 權限 in-code 注入

**Files:**
- Modify: `<BE>/utils/permissions.py`（enum ~92、SPLIT_MODULES ~207、ROLE_TEMPLATES hr~242/supervisor~296/accountant~364、PERMISSION_LABELS ~455、PERMISSION_GROUPS ~566）
- Test: `<BE>/tests/test_misc_receipts.py`（加權限常數測試）

**Interfaces:**
- Produces: `Permission.MISC_RECEIPT_READ` / `Permission.MISC_RECEIPT_WRITE`（str enum，值同名）。

- [ ] **Step 1: 寫失敗測試**（append 到 `<BE>/tests/test_misc_receipts.py`）

```python
def test_misc_receipt_permissions_exist():
    from utils.permissions import Permission, PERMISSION_LABELS
    assert Permission.MISC_RECEIPT_READ.value == "MISC_RECEIPT_READ"
    assert Permission.MISC_RECEIPT_WRITE.value == "MISC_RECEIPT_WRITE"
    assert PERMISSION_LABELS["MISC_RECEIPT_READ"] == "雜項收款 (檢視)"
    assert PERMISSION_LABELS["MISC_RECEIPT_WRITE"] == "雜項收款 (編輯/簽收)"


def test_misc_receipt_in_finance_roles():
    from utils.permissions import ROLE_TEMPLATES
    for role in ("hr", "supervisor", "accountant"):
        assert "MISC_RECEIPT_READ" in ROLE_TEMPLATES[role]
        assert "MISC_RECEIPT_WRITE" in ROLE_TEMPLATES[role]
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py -o addopts="" -q`
Expected: FAIL（AttributeError / KeyError）

- [ ] **Step 3: 加 enum**（緊接 `VENDOR_PAYMENT_WRITE` 那行之後，`utils/permissions.py` ~93）

```python
    MISC_RECEIPT_READ = "MISC_RECEIPT_READ"
    MISC_RECEIPT_WRITE = "MISC_RECEIPT_WRITE"
```

> **不要**加進 `LEGACY_PERMISSION_BITS`（已凍結，bit 已用盡到 `1<<62`；text-array 系統不需要 bit）。

- [ ] **Step 4: 加 SPLIT_MODULES**（緊接 `VENDOR_PAYMENT` 區塊後，~210）

```python
    "MISC_RECEIPT": {
        "read": "MISC_RECEIPT_READ",
        "write": "MISC_RECEIPT_WRITE",
    },
```

- [ ] **Step 5: 加 PERMISSION_LABELS**（緊接廠商付款兩行後，~457）

```python
    # 雜項收款
    "MISC_RECEIPT_READ": "雜項收款 (檢視)",
    "MISC_RECEIPT_WRITE": "雜項收款 (編輯/簽收)",
```

- [ ] **Step 6: 加 PERMISSION_GROUPS**（緊接「廠商付款簽收」module 區塊後，~570，同「公告 / 行政」群組）

```python
            {
                "module": "雜項收款簽收",
                "read": "MISC_RECEIPT_READ",
                "write": "MISC_RECEIPT_WRITE",
            },
```

- [ ] **Step 7: 加 ROLE_TEMPLATES**（hr ~244、supervisor ~298 各加兩行；accountant 在 `ROLE_TEMPLATES["accountant"] = [...]` ~365 加兩行）

hr 與 supervisor 區塊（緊接該角色的 `VENDOR_PAYMENT_WRITE.value,` 後）：
```python
        # 雜項收款：比照廠商付款權限
        Permission.MISC_RECEIPT_READ.value,
        Permission.MISC_RECEIPT_WRITE.value,
```
accountant（`ROLE_TEMPLATES["accountant"]` list 內，緊接其 `VENDOR_PAYMENT_WRITE.value,` 後）：
```python
    Permission.MISC_RECEIPT_READ.value,
    Permission.MISC_RECEIPT_WRITE.value,
```

> admin 是 `["*"]` wildcard，自動涵蓋，不需改。principal = supervisor + extras，自動繼承。

- [ ] **Step 8: 跑測試確認 pass**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 9: commit**

```bash
cd <BE>
git commit utils/permissions.py tests/test_misc_receipts.py -m "feat(misc-receipts): 新增雜項收款權限與角色映射"
```

---

## Task 4: 權限 seed Migration

**Files:**
- Create: `<BE>/alembic/versions/20260629_mscrcptp01_seed_misc_receipt_perms.py`

**Interfaces:**
- Consumes: revision `mscrcpt01`；`utils.permission_backfill.sync_core_role_permissions`；Task 3 的 in-code ROLE_TEMPLATES。
- Produces: revision `mscrcptp01`；prod `permission_definitions` 兩列 + `roles` array 補授。

- [ ] **Step 1: 寫 migration**（`<BE>/alembic/versions/20260629_mscrcptp01_seed_misc_receipt_perms.py`）

```python
"""seed 雜項收款 permission_definitions + roles 補授

Revision ID: mscrcptp01
Revises: mscrcpt01
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "mscrcptp01"
down_revision = "mscrcpt01"
branch_labels = None
depends_on = None

_DEFS = [
    {"code": "MISC_RECEIPT_READ", "label": "雜項收款 (檢視)", "group_name": "公告 / 行政"},
    {"code": "MISC_RECEIPT_WRITE", "label": "雜項收款 (編輯/簽收)", "group_name": "公告 / 行政"},
]


def upgrade() -> None:
    from utils.permission_backfill import sync_core_role_permissions

    conn = op.get_bind()
    existing = {r[0] for r in conn.execute(sa.text("SELECT code FROM permission_definitions"))}
    rows = [d for d in _DEFS if d["code"] not in existing]
    if rows:
        conn.execute(
            sa.text(
                "INSERT INTO permission_definitions (code, label, group_name, is_core) "
                "VALUES (:code, :label, :group_name, true)"
            ),
            rows,
        )
    # 讀 in-code ROLE_TEMPLATES（Task 3 已加 hr/supervisor/accountant），補進 DB roles array
    sync_core_role_permissions(conn)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM permission_definitions "
            "WHERE code IN ('MISC_RECEIPT_READ','MISC_RECEIPT_WRITE')"
        )
    )
```

> **group_name 必須與 VENDOR_PAYMENT 在 `permission_definitions` 的 group_name 一致**。執行時先 `SELECT group_name FROM permission_definitions WHERE code='VENDOR_PAYMENT_READ'` 核對，若非「公告 / 行政」則改成實際值。downgrade 不移除 roles array 內的碼（additive，比照 permbf01 的 no-op 慣例；移除非必要且風險低）。

- [ ] **Step 2: 驗證 head + roundtrip**

Run: `cd <BE> && python -m alembic heads && python -m alembic upgrade head && python -m alembic downgrade -1 && python -m alembic upgrade head`
Expected: head single（`mscrcptp01`）；無錯。

- [ ] **Step 3: commit**

```bash
cd <BE>
git commit alembic/versions/20260629_mscrcptp01_seed_misc_receipt_perms.py -m "feat(misc-receipts): seed 雜項收款權限定義與角色"
```

---

## Task 5: Response Schemas

**Files:**
- Create: `<BE>/schemas/misc_receipts.py`

**Interfaces:**
- Produces: `MiscReceiptAttachmentMetaOut`、`MiscReceiptOut`、`MiscReceiptListOut`、`MiscReceiptSummaryOut`；re-export `DeleteResultOut`、`MutationResultOut`。

- [ ] **Step 1: 建立 schemas**（`<BE>/schemas/misc_receipts.py`）— 鏡像 `schemas/vendor_payments.py`，套替換表 + 加 `category`

```python
"""雜項收款簽收 router (api/misc_receipts.py) 對應 Out schemas。

涵蓋（全 admin 後台，無公開）：
- GET  /misc-receipts                          → MiscReceiptListOut
- GET  /misc-receipts/{receipt_id}             → MiscReceiptOut
- GET  /misc-receipts/summary                  → MiscReceiptSummaryOut
- POST /misc-receipts/{receipt_id}/attachments → MiscReceiptAttachmentMetaOut

PII 註解：payer_name / amount / receipt_number / description / notes /
signer_name / created_by_name 均業務必看（非跨人 PII），substring 命中
denylist 故標 pii-allow（同廠商付款 schema 機制）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel
from schemas._common import (  # noqa: F401
    DeleteResultOut,
    MutationResultOut,
)


class MiscReceiptAttachmentMetaOut(IvyBaseModel):
    """雜項收款附件 metadata（單筆）。"""

    key: str
    filename: str  # pii-allow: 原始上傳檔名（行政帳務必看）
    size: int
    mime_type: Optional[str] = None
    uploaded_at: Optional[str] = None
    uploaded_by_id: Optional[int] = None


class MiscReceiptOut(IvyBaseModel):
    """單筆雜項收款（含簽收狀態 / 附件 metadata）。對應 router _to_dict(row)。"""

    id: int
    receipt_date: Optional[str] = None
    payer_name: str  # pii-allow: 繳款方名稱（行政帳務必看）
    category: str
    amount: Optional[float] = None  # pii-allow: 收款金額（業務需看）
    payment_method: str
    description: Optional[str] = None  # pii-allow: 行政自填說明
    receipt_number: Optional[str] = None  # pii-allow: 收據/單據號碼
    notes: Optional[str] = None  # pii-allow: 行政自填備註
    attachments: list[MiscReceiptAttachmentMetaOut]
    status: str
    signer_id: Optional[int] = None
    signer_name: Optional[str] = None  # pii-allow: 內部員工姓名（自家後台必顯示）
    signed_at: Optional[str] = None
    signature_kind: Optional[str] = None
    has_signature: bool
    created_by_id: Optional[int] = None
    created_by_name: Optional[str] = None  # pii-allow: 內部員工姓名
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MiscReceiptListOut(IvyBaseModel):
    """GET /misc-receipts 分頁列表回傳。"""

    items: list[MiscReceiptOut]
    total: int
    page: int
    page_size: int


class MiscReceiptSummaryOut(IvyBaseModel):
    """GET /misc-receipts/summary 區間彙總（KPI 卡，跨狀態，含 pending）。"""

    total_count: int
    total_amount: float  # pii-allow: 收款金額彙總
    pending_count: int
    pending_amount: float  # pii-allow
    signed_count: int
    signed_amount: float  # pii-allow
```

- [ ] **Step 2: import smoke 驗證**

Run: `cd <BE> && python -c "from schemas.misc_receipts import MiscReceiptOut, MiscReceiptListOut, MiscReceiptSummaryOut, MiscReceiptAttachmentMetaOut; print('ok')"`
Expected: 印出 `ok`

- [ ] **Step 3: commit**

```bash
cd <BE>
git commit schemas/misc_receipts.py -m "feat(misc-receipts): 新增雜項收款 Out schemas"
```

---

## Task 6: Router + 註冊（CRUD/summary/sign/attachments）

**Files:**
- Create: `<BE>/api/misc_receipts.py`（鏡像 `api/vendor_payments.py`，套替換表 + 加 `category`）
- Modify: `<BE>/main.py`（import + include_router）
- Test: `<BE>/tests/test_misc_receipts.py`（端點/權限/簽收/終態/日期守衛）

**Interfaces:**
- Consumes: `MiscReceipt` model、`schemas.misc_receipts.*`、`Permission.MISC_RECEIPT_*`、`utils.finance_cache.invalidate_finance_summary_cache`、`utils.portfolio_storage.get_portfolio_storage`、`utils.taipei_time.validate_payment_date`。
- Produces: router `api.misc_receipts.router`（prefix `/api`, tag `misc-receipts`）；11 端點對稱 vendor_payments。

- [ ] **Step 1: 鏡像 router 檔**

複製 `<BE>/api/vendor_payments.py` → `<BE>/api/misc_receipts.py`，對全檔套「識別碼替換對照表」，並做以下**收入側差異**：

1. **常數**：`PAYMENT_DATE_BACK_LIMIT_DAYS = 90` 改名 `RECEIPT_DATE_BACK_LIMIT_DAYS = 90`（語義；值不變）。`MAX_ATTACHMENTS_PER_PAYMENT` → `MAX_ATTACHMENTS_PER_RECEIPT`。
2. **新增 `category` 校驗**：在 Create schema 加 `category: str` 欄位 + validator：
   ```python
   from models.misc_receipt import RECEIPT_CATEGORIES

       @field_validator("category")
       @classmethod
       def guard_category(cls, v: str) -> str:
           if v not in RECEIPT_CATEGORIES:
               raise ValueError(f"category 必須是 {RECEIPT_CATEGORIES} 其一")
           return v
   ```
   Update schema 的 `category: Optional[str]` 同樣 validator（None 放行）。
3. **`_to_dict(row)`**：輸出加 `"category": row.category`。
4. **list 端點**：query 參數加 `category: Optional[str] = None`，並在 filter 加 `if category: q = q.filter(MiscReceipt.category == category)`。
5. **summary 端點**：range 篩選參數 `vendor_name` → `payer_name`，並加 `category`（與 list 一致的 range 篩選，但**不**吃 status）。
6. **日期 validator**：呼叫 `validate_payment_date(v, back_limit_days=RECEIPT_DATE_BACK_LIMIT_DAYS)`（函式名不變，沿用 `utils.taipei_time`）。
7. **審計摘要文案**：「簽收廠商付款 #」→「簽收雜項收款 #」等所有「廠商付款」→「雜項收款」。
8. **finance cache wrapper**：保留 `_invalidate_finance_cache()` → `invalidate_finance_summary_cache()`，呼叫點同源（create/update/delete）。
9. **權限 Depends**：`Permission.VENDOR_PAYMENT_*` → `Permission.MISC_RECEIPT_*`（替換表已涵蓋）。
10. **response_model**：各端點 `response_model=MiscReceiptListOut / MiscReceiptOut / MiscReceiptSummaryOut / MiscReceiptAttachmentMetaOut`（import from `schemas.misc_receipts`）。

> 簽名解析 `_parse_signature_payload`、storage `get_portfolio_storage().put_attachment`、附件 list-replacement `row.attachments = existing + [meta]`、終態 `status != "pending"` → 409 守衛、`raise_safe_500` 全部原樣鏡像，無收入側差異。

- [ ] **Step 2: 註冊到 main.py**

`<BE>/main.py` import 區（~27，vendor_payments import 旁）：
```python
from api.misc_receipts import router as misc_receipts_router
```
include 區（~1185，vendor_payments include 旁）：
```python
app.include_router(misc_receipts_router)
```

- [ ] **Step 3: 寫端點測試**（append `<BE>/tests/test_misc_receipts.py`）

```python
from fastapi.testclient import TestClient


def test_create_sign_lifecycle(client_with_write_perm):
    """建立 → pending → 簽收 → signed → 終態鎖定。"""
    client = client_with_write_perm
    body = {
        "receipt_date": "2026-06-01", "payer_name": "某基金會", "category": "donation",
        "amount": 5000, "payment_method": "bank_transfer", "description": "六月捐款",
    }
    r = client.post("/api/misc-receipts", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "pending"
    assert r.json()["category"] == "donation"

    # 簽收
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    r2 = client.post(f"/api/misc-receipts/{rid}/sign",
                     json={"signature_kind": "photo", "signature_data": tiny_png})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "signed"

    # 終態鎖定：已簽收不可編輯
    r3 = client.put(f"/api/misc-receipts/{rid}", json={"amount": 9999})
    assert r3.status_code == 409


def test_create_rejects_bad_category(client_with_write_perm):
    r = client_with_write_perm.post("/api/misc-receipts", json={
        "receipt_date": "2026-06-01", "payer_name": "X", "category": "bogus",
        "amount": 100, "payment_method": "cash",
    })
    assert r.status_code == 422


def test_create_rejects_future_date(client_with_write_perm):
    r = client_with_write_perm.post("/api/misc-receipts", json={
        "receipt_date": "2099-01-01", "payer_name": "X", "category": "other",
        "amount": 100, "payment_method": "cash",
    })
    assert r.status_code == 422


def test_read_requires_permission(client_without_perm):
    r = client_without_perm.get("/api/misc-receipts")
    assert r.status_code in (401, 403)
```

> **fixtures**：`client_with_write_perm` / `client_without_perm` 比照 `tests/test_vendor_payments.py` 既有的 auth fixture 寫法（grep `vendor_payments` 在 tests/ 找對應 conftest fixture 名，直接重用或鏡像）。務必掛 `test_db_session`。

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py -o addopts="" -q`
Expected: PASS（全綠）

- [ ] **Step 5: commit**

```bash
cd <BE>
git add api/misc_receipts.py main.py tests/test_misc_receipts.py
git commit api/misc_receipts.py main.py tests/test_misc_receipts.py -m "feat(misc-receipts): 新增雜項收款 router 與端點測試"
```

---

## Task 7: 財報收入接入

**Files:**
- Modify: `<BE>/services/finance_report_service.py`（import ~34、新 provider ~225 後、`build_finance_summary` ~586/592/630、`build_finance_detail` ~834 可選）
- Test: `<BE>/tests/test_misc_receipt_finance.py`

**Interfaces:**
- Consumes: `MiscReceipt` model、現有 `_year_range` / `_month_totals_from` / `extract` / `func`。
- Produces: `get_misc_receipt_revenue_by_month(session, year) -> dict[int, int]`；`build_finance_summary` 的 `revenue` 與 `revenue_by_category` 含 misc。

- [ ] **Step 1: 寫失敗測試**（`<BE>/tests/test_misc_receipt_finance.py`）

```python
from datetime import date
from models.misc_receipt import MiscReceipt
from services.finance_report_service import get_misc_receipt_revenue_by_month


def test_misc_revenue_aggregates_by_month_including_pending(test_db_session):
    test_db_session.add_all([
        MiscReceipt(receipt_date=date(2026, 3, 5), payer_name="A", category="rent",
                    amount=1000, payment_method="cash", status="pending", attachments=[]),
        MiscReceipt(receipt_date=date(2026, 3, 20), payer_name="B", category="donation",
                    amount=500, payment_method="cash", status="signed", attachments=[]),
        MiscReceipt(receipt_date=date(2026, 4, 1), payer_name="C", category="other",
                    amount=200, payment_method="cash", status="pending", attachments=[]),
    ])
    test_db_session.flush()
    result = get_misc_receipt_revenue_by_month(test_db_session, 2026)
    assert result.get(3) == 1500  # pending + signed 都計入
    assert result.get(4) == 200
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd <BE> && python -m pytest tests/test_misc_receipt_finance.py -o addopts="" -q`
Expected: FAIL（ImportError：`get_misc_receipt_revenue_by_month`）

- [ ] **Step 3: 加 import + provider**

`<BE>/services/finance_report_service.py` import 區（~34，vendor_payment import 旁）：
```python
from models.misc_receipt import MiscReceipt
```
新 provider（緊接 `get_vendor_payment_expense_by_month` 後，~225）：
```python
def get_misc_receipt_revenue_by_month(session: Session, year: int) -> dict[int, int]:
    """雜項收款收入，按 receipt_date 月份聚合。
    無論 status 為 pending 或 signed 都計入（與廠商付款支出口徑對齊）。"""
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", MiscReceipt.receipt_date).label("m"),
            func.sum(MiscReceipt.amount),
        )
        .filter(
            MiscReceipt.receipt_date >= start,
            MiscReceipt.receipt_date < end,
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)
```

- [ ] **Step 4: 接入 build_finance_summary**

在 `build_finance_summary` 內：
- 取得月聚合（vendor_exp 那行旁，~586）：
  ```python
  misc_rev = get_misc_receipt_revenue_by_month(session, year)
  ```
- 每月 revenue 累加（~592）改成：
  ```python
  revenue = tuition_rev.get(m, 0) + activity_rev.get(m, 0) + misc_rev.get(m, 0)
  ```
- 算總額（與 tuition_rev_total / activity_rev_total 同處）：
  ```python
  misc_rev_total = sum(misc_rev.values())
  ```
- `revenue_by_category` list（~630）加一項（雜項收款無退款概念，refund=0）：
  ```python
  {"category": "misc_receipt", "label": "雜項收款", "amount": misc_rev_total, "refund": 0},
  ```

- [ ] **Step 5:（可選）下鑽 detail 接入**

若財報有下鑽（build_finance_detail），鏡像 `get_vendor_payment_detail`（~807）成 `get_misc_receipt_detail`，並在 `build_finance_detail`（~834）加 key `"misc_receipt"`。若 KPI 卡不需 misc 下鑽則跳過，於 commit message 註明「detail 下鑽未接，列 follow-up」。

- [ ] **Step 6: 跑測試確認 pass**

Run: `cd <BE> && python -m pytest tests/test_misc_receipt_finance.py tests/test_misc_receipts.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 7: 跑既有財報測試確認未回歸**

Run: `cd <BE> && python -m pytest tests/ -k "finance" -o addopts="" -q`
Expected: PASS（既有財報測試全綠）

- [ ] **Step 8: commit**

```bash
cd <BE>
git commit services/finance_report_service.py tests/test_misc_receipt_finance.py -m "feat(misc-receipts): 雜項收款納入財報收入聚合"
```

---

## Task 8: 後端整體驗證 + OpenAPI 產出

**Files:** 無（驗證步驟）

- [ ] **Step 1: 跑後端相關測試全綠**

Run: `cd <BE> && python -m pytest tests/test_misc_receipts.py tests/test_misc_receipt_finance.py tests/test_vendor_payments.py -o addopts="" -q`
Expected: PASS（含廠商付款回歸，確認未誤傷）

- [ ] **Step 2: 產 openapi.json（給前端 codegen）**

Run: `cd <BE> && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only，.gitignore 擋，不 commit）。確認含 `/misc-receipts` 路徑。

---

## Task 9: 前端 worktree + api wrapper + codegen

**Files:**
- Create: `<FE>/src/api/miscReceipt.ts`
- Modify: `<FE>/src/api/_generated/schema.d.ts`（codegen 產出）
- Test: `<FE>/src/api/__tests__/miscReceipt.spec.ts`

**Interfaces:**
- Produces: `listMiscReceipts` / `getMiscReceiptSummary` / `getMiscReceipt` / `createMiscReceipt` / `updateMiscReceipt` / `deleteMiscReceipt` / `signMiscReceipt` / `uploadMiscReceiptAttachment` / `deleteMiscReceiptAttachment` / `downloadMiscReceiptAttachmentUrl` / `miscReceiptSignatureUrl` / `PAYMENT_METHOD_OPTIONS` / `paymentMethodLabel` / `CATEGORY_OPTIONS` / `categoryLabel`。

- [ ] **Step 1: 開前端 worktree**（見 Execution Setup #3）

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend worktree add .claude/worktrees/misc-receipt -b feat/misc-receipt-signoff <base>
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/misc-receipt && ln -s ../../../node_modules node_modules
```

- [ ] **Step 2: codegen schema.d.ts**

把 Task 8 產的 `openapi.json` 複製到前端 worktree（依 `npm run gen:api` 預期路徑），跑：
Run: `cd <FE> && npm run gen:api`
Expected: `src/api/_generated/schema.d.ts` 更新，含 misc-receipts 型別。

- [ ] **Step 3: 建立 api wrapper**（`<FE>/src/api/miscReceipt.ts`）— 鏡像 `vendorPayment.ts` + 加 CATEGORY_OPTIONS

```ts
import api, { API_BASE } from './index'

/** 區間彙總（跨狀態），對應後端 GET /misc-receipts/summary。 */
export interface MiscReceiptSummary {
  total_count: number
  total_amount: number
  pending_count: number
  pending_amount: number
  signed_count: number
  signed_amount: number
}

export const listMiscReceipts = (params: unknown) => api.get('/misc-receipts', { params })

export const getMiscReceiptSummary = (params?: unknown) =>
  api.get('/misc-receipts/summary', { params })

export const getMiscReceipt = (id: number) => api.get(`/misc-receipts/${id}`)

export const createMiscReceipt = (data: unknown) => api.post('/misc-receipts', data)

export const updateMiscReceipt = (id: number, data: unknown) =>
  api.put(`/misc-receipts/${id}`, data)

export const deleteMiscReceipt = (id: number) => api.delete(`/misc-receipts/${id}`)

export const signMiscReceipt = (id: number, data: unknown) =>
  api.post(`/misc-receipts/${id}/sign`, data)

export const uploadMiscReceiptAttachment = (id: number, formData: FormData) =>
  api.post(`/misc-receipts/${id}/attachments`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })

export const deleteMiscReceiptAttachment = (id: number, key: string) =>
  api.delete(`/misc-receipts/${id}/attachments`, { params: { key } })

export const downloadMiscReceiptAttachmentUrl = (id: number, key: string) =>
  `${API_BASE}/misc-receipts/${id}/attachments/download?key=${encodeURIComponent(key)}`

export const miscReceiptSignatureUrl = (id: number) =>
  `${API_BASE}/misc-receipts/${id}/signature`

export const PAYMENT_METHOD_OPTIONS = [
  { value: 'cash', label: '現金' },
  { value: 'bank_transfer', label: '銀行匯款' },
  { value: 'check', label: '支票' },
  { value: 'linepay', label: 'LINE Pay' },
  { value: 'other', label: '其他' },
]

export const paymentMethodLabel = (value: string) =>
  PAYMENT_METHOD_OPTIONS.find((o) => o.value === value)?.label || value

export const CATEGORY_OPTIONS = [
  { value: 'rent', label: '場地租金' },
  { value: 'donation', label: '捐款' },
  { value: 'subsidy', label: '補助款' },
  { value: 'secondhand_sale', label: '二手義賣' },
  { value: 'refund_recovery', label: '退費回收' },
  { value: 'other', label: '其他' },
]

export const categoryLabel = (value: string) =>
  CATEGORY_OPTIONS.find((o) => o.value === value)?.label || value
```

- [ ] **Step 4: 寫測試**（`<FE>/src/api/__tests__/miscReceipt.spec.ts`）

```ts
import { describe, it, expect } from 'vitest'
import { CATEGORY_OPTIONS, categoryLabel, paymentMethodLabel } from '../miscReceipt'

describe('miscReceipt api helpers', () => {
  it('exposes 6 categories', () => {
    expect(CATEGORY_OPTIONS.map((o) => o.value)).toEqual([
      'rent', 'donation', 'subsidy', 'secondhand_sale', 'refund_recovery', 'other',
    ])
  })
  it('maps category value to label', () => {
    expect(categoryLabel('donation')).toBe('捐款')
    expect(categoryLabel('unknown')).toBe('unknown')
  })
  it('maps payment method to label', () => {
    expect(paymentMethodLabel('linepay')).toBe('LINE Pay')
  })
})
```

- [ ] **Step 5: 跑測試 + typecheck**

Run: `cd <FE> && npx vitest run src/api/__tests__/miscReceipt.spec.ts && npm run typecheck`
Expected: PASS + typecheck 無錯。

- [ ] **Step 6: commit**

```bash
cd <FE>
git add src/api/miscReceipt.ts src/api/__tests__/miscReceipt.spec.ts src/api/_generated/schema.d.ts
git commit src/api/miscReceipt.ts src/api/__tests__/miscReceipt.spec.ts src/api/_generated/schema.d.ts -m "feat(misc-receipts): 新增雜項收款 api wrapper 與型別"
```

---

## Task 10: 前端路由 / 權限 / 側邊欄註冊

**Files:**
- Modify: `<FE>/src/constants/permissions.ts`（PERMISSION_NAMES ~66、ROUTE_PERMISSION_RULES ~164）
- Modify: `<FE>/src/router/index.ts`（~196）
- Modify: `<FE>/src/components/layout/AdminSidebar.vue`（~123）

**Interfaces:**
- Consumes: `MiscReceiptView.vue`（Task 11，先用佔位或同 commit）。
- Produces: 路由 `/misc-receipts`、權限碼 `MISC_RECEIPT_*`、側邊欄入口。

- [ ] **Step 1: PERMISSION_NAMES**（`<FE>/src/constants/permissions.ts` ~67，VENDOR_PAYMENT 兩行後）

```ts
  MISC_RECEIPT_READ: 'MISC_RECEIPT_READ',
  MISC_RECEIPT_WRITE: 'MISC_RECEIPT_WRITE',
```

- [ ] **Step 2: ROUTE_PERMISSION_RULES**（~165，vendor-payments rule 後）

```ts
  // 雜項收款簽收（園務行政）
  { path: '/misc-receipts', permission: 'MISC_RECEIPT_READ' },
```

- [ ] **Step 3: router**（`<FE>/src/router/index.ts` ~201，vendor-payments route 後）

```ts
        {
            path: '/misc-receipts',
            name: 'misc-receipts',
            component: () => import('../views/MiscReceiptView.vue'),
            meta: { title: '雜項收款簽收' }
        },
```

- [ ] **Step 4: 側邊欄**（`<FE>/src/components/layout/AdminSidebar.vue` ~126，廠商付款 menu-item 後，同「公告/行政」子選單內。用不同 el-icon，Money 已被佔用，改用 `Wallet` 或 `Coin`）

```vue
          <el-menu-item v-if="canView.MISC_RECEIPT_READ" index="/misc-receipts">
            <el-icon><Wallet /></el-icon>
            <template #title>雜項收款簽收</template>
          </el-menu-item>
```
確認 `Wallet`（或所選 icon）已從 `@element-plus/icons-vue` import；若無則在該檔 import 區補上。`canView.MISC_RECEIPT_READ` 因 Step 1 已自動可用，無需改 script。

- [ ] **Step 5: typecheck**

Run: `cd <FE> && npm run typecheck`
Expected: 無錯（MiscReceiptView 若尚未建立，先建空殼或與 Task 11 同 commit；建議 Task 11 先做完再跑此步）。

- [ ] **Step 6: commit**

```bash
cd <FE>
git add src/constants/permissions.ts src/router/index.ts src/components/layout/AdminSidebar.vue
git commit src/constants/permissions.ts src/router/index.ts src/components/layout/AdminSidebar.vue -m "feat(misc-receipts): 註冊雜項收款路由/權限/側邊欄"
```

---

## Task 11: MiscReceiptView 主頁面

**Files:**
- Create: `<FE>/src/views/MiscReceiptView.vue`（鏡像 `VendorPaymentView.vue` + category）

**Interfaces:**
- Consumes: `src/api/miscReceipt.ts` 全部 export、`MiscReceiptSignDialog.vue`（Task 12）。

- [ ] **Step 1: 鏡像 View 檔**

複製 `<FE>/src/views/VendorPaymentView.vue` → `<FE>/src/views/MiscReceiptView.vue`，套替換表，並做收入側差異：

1. import 從 `'../api/vendorPayment'` → `'../api/miscReceipt'`，加 `CATEGORY_OPTIONS, categoryLabel`。
2. 簽收彈窗元件 import → `MiscReceiptSignDialog`。
3. 表單欄位「廠商名稱」→「繳款方/來源」（label，綁 `payer_name`）；「發票號碼」→「收據/單據號」（綁 `receipt_number`）；「付款日期」→「收款日期」（綁 `receipt_date`）。
4. **新增 `category` 欄位**：新增/編輯 Dialog 加 `el-select` 綁 `form.category`，options = `CATEGORY_OPTIONS`，必填校驗。
5. **表格加「類別」欄**：`<el-table-column label="類別">` 用 `categoryLabel(row.category)`；篩選列加類別 `el-select`（值傳入 list 的 `category` 參數）。
6. KPI 卡文案「廠商付款」→「雜項收款」；頁標題、按鈕「新增付款」→「新增收款」。
7. 列表 query 帶 `category` 篩選參數。
8. 所有「廠商付款」文案 → 「雜項收款」；「簽收」流程文案保留。

> 表格、分頁、Dialog 開關、附件上傳/下載、簽收按鈕邏輯全部原樣鏡像。`<script setup lang="ts">`，禁 `any`。

- [ ] **Step 2: typecheck + build**

Run: `cd <FE> && npm run typecheck && npm run build`
Expected: typecheck 無錯、build 成功。

- [ ] **Step 3: commit**

```bash
cd <FE>
git add src/views/MiscReceiptView.vue
git commit src/views/MiscReceiptView.vue -m "feat(misc-receipts): 新增雜項收款主頁面"
```

---

## Task 12: MiscReceiptSignDialog 簽收彈窗

**Files:**
- Create: `<FE>/src/components/MiscReceiptSignDialog.vue`（鏡像 `VendorPaymentSignDialog.vue`）

**Interfaces:**
- Consumes: `signMiscReceipt` from `src/api/miscReceipt.ts`。

- [ ] **Step 1: 鏡像簽收彈窗**

複製 `<FE>/src/components/VendorPaymentSignDialog.vue` → `<FE>/src/components/MiscReceiptSignDialog.vue`，套替換表：

1. import `signVendorPayment` → `signMiscReceipt`（from `'../api/miscReceipt'`）。
2. props / emit 介面不變（傳 receipt id、emit success）。
3. 兩個 tab「上傳紙本照片」/「當場手寫」canvas 邏輯、dataURL 壓縮、提交全部原樣。
4. 文案「廠商付款」→「雜項收款」、「廠商簽收」→「繳款方簽收」。

> 確認 Task 11 的 View 已正確 import 此元件名 `MiscReceiptSignDialog`。

- [ ] **Step 2: typecheck + 既有測試樹掃描**

Run: `cd <FE> && npm run typecheck && npx vitest run src/components`
Expected: typecheck 無錯；既有 component 測試未因新增檔案而破壞。

- [ ] **Step 3: commit**

```bash
cd <FE>
git add src/components/MiscReceiptSignDialog.vue
git commit src/components/MiscReceiptSignDialog.vue -m "feat(misc-receipts): 新增雜項收款簽收彈窗"
```

---

## Task 13: 整合驗證 + 收尾

**Files:** 無（驗證 + 收尾）

- [ ] **Step 1: 起兩端 dev server**

> ⚠ `start.sh` 是長駐前景 launcher，**勿** background/`&`（會搶 port、孤兒 daemon）。在另一終端跑：
```bash
cd /Users/yilunwu/Desktop/ivyManageSystem && ./start.sh
```
（注意：worktree 不是 start.sh 預期的 repo 路徑；整合驗證可改在主 checkout 把 feature 分支 checkout 出來跑，或手動起 worktree 內的 uvicorn + vite。執行時依環境選最簡路徑。）

- [ ] **Step 2: 實際點一次（admin 登入）**

dev 帳號 `admin` / `ivytest123`（見 workspace 記憶）。手動驗證：
1. 側邊欄出現「雜項收款簽收」。
2. 新增一筆收款（選類別、填繳款方/金額/收款日）→ 出現在列表，status=待簽收。
3. 簽收（上傳一張圖）→ status=已簽收，列表更新。
4. 已簽收嘗試編輯 → 被擋（409 / UI 提示）。
5. 財報頁（若有）收入區出現「雜項收款」一列、金額正確。

- [ ] **Step 3: 跑前端全相關測試樹**

Run: `cd <FE> && npx vitest run src/api src/components src/views/__tests__ 2>/dev/null; npm run typecheck`
Expected: 綠 + typecheck 無錯。

- [ ] **Step 4: 收尾（Definition of Done）**

依 workspace 收尾紀律「完成 = push + CI 綠 + worktree remove」：
1. 後端：`git -C <BE> push origin feat/misc-receipt-signoff`（⚠ push 後端 = 觸發 Zeabur 部署 + 跑 migration；確認 prod 前置，尤其 `permission_definitions` seed 先於前端拉新欄位）。或依 user 指示先併 local main / 開 PR。
2. 前端：`git -C <FE> push origin feat/misc-receipt-signoff`。
3. **push / 併分支策略以 user 指示為準**（本 workspace 常態是「併 local main 未 push」+ user 親自 push 觸發部署）。**先問 user** 要 push、開 PR、還是併 local main。
4. CI 綠後 `git worktree remove` 兩個 worktree。

---

## Self-Review（against spec）

- **Spec §4 資料模型** → Task 1（model）+ Task 2（migration），含 `category` 與所有改名欄位。✓
- **Spec §5 簽收語義/終態鎖定** → Task 6（鏡像 sign + 409 守衛）+ Task 6 測試。✓
- **Spec §6 API 契約（11 端點）** → Task 6（鏡像全端點 + category 篩選）。✓
- **Spec §7 權限** → Task 3（in-code）+ Task 4（prod seed migration）。✓
- **Spec §8 財報收入（含 pending）** → Task 7，口徑明確含 pending。✓
- **Spec §9 共用策略**（後端 helper 沿用 `utils.*`、前端 UI 各自獨立）→ Task 6 沿用 `taipei_time`/`portfolio_storage`/`finance_cache`；前端 Task 11/12 獨立檔。✓
- **Spec §10 前端** → Task 9（api）/10（註冊）/11（View）/12（Dialog）。✓
- **Spec §11 測試** → 各 Task 內嵌 TDD + Task 13 整合。✓
- **Spec §12 Migration**（建表 + 權限 seed + roles）→ Task 2 + Task 4。✓
- **Spec §13 實作注意（共用 checkout/分開 commit/prod 部署/DoD）** → Global Constraints + Execution Setup + Task 13。✓
- **Spec §14 開放問題**：①類別清單已定 6 類（Task 1）②財報口徑含 pending（Task 7）③回補上限沿用 90 天（Task 6）④共用策略後端 helper 共用/前端 UI 獨立（已確認）。✓

**Placeholder scan**：Task 5（detail 下鑽）標為可選並要求 commit message 註明 follow-up，非空泛 placeholder。fixtures 名（Task 6 Step 3）指向既有 `tests/test_vendor_payments.py` 重用，非 TBD。✓

**Type consistency**：provider `get_misc_receipt_revenue_by_month`（Task 7）、api export 名（Task 9）、權限碼 `MISC_RECEIPT_READ/WRITE`、欄位名 `receipt_date/payer_name/category/receipt_number` 全文一致。✓
