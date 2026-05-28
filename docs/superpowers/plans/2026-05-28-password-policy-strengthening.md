# 密碼政策強化 ≥12 + HIBP + password_history Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `_PASSWORD_MIN_LENGTH` 8 → 12 + HIBP k-anonymity check（fail-open）+ password_history 表防重用最近 5 個密碼。

**Architecture:** 新 `utils/hibp.py` 提供 `assert_not_pwned(password)` 走 api.pwnedpasswords.com 5-char SHA-1 prefix query；新 `utils/password_history.py` 提供 `assert_not_recently_used(session, user_id, plaintext)` + `record(session, user_id, hash)`；新 Alembic migration `pwdhist01` 建 `password_history` 表 + ORM class；3 個 password mutation endpoint (change/create/reset) 接 helper。

**Tech Stack:** Python 3.9 / FastAPI / SQLAlchemy / Alembic / pytest / requests (既有)

**Spec:** `docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md`

**重要 context：**
- 當前 git branch 應為 `feat/password-policy-strengthening-2026-05-28-backend`（spec commit `67993df`）。執行前 `git branch --show-current` 確認
- Branch base 是 origin/main，**不含** sub-project #1-3 改動
- Alembic single head 為 `intghealth01`（plan 開始前再 `python3 -c "from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())"` 確認；若 user 並行加新 head 需 merge）
- Backend working tree 可能有 user 並行 WIP（`pyproject.toml`）。**不 stash、不 `git add -A`**

---

## File Structure

```
ivy-backend/
├── utils/
│   ├── auth.py                                            ← Task 5 (Modify _PASSWORD_MIN_LENGTH + validate_password_strength body)
│   ├── hibp.py                                            ← Task 2 (Create ~75 行)
│   └── password_history.py                                ← Task 4 (Create ~55 行)
├── models/
│   └── auth.py                                            ← Task 3 (Modify: add PasswordHistory ORM class)
├── alembic/versions/
│   └── 20260528_pwdhist01_password_history_table.py       ← Task 1 (Create migration)
├── api/
│   └── auth.py                                            ← Task 6 (Modify 3 endpoints: change/create/reset)
└── tests/
    ├── test_hibp.py                                       ← Task 2 (Create 4 tests)
    ├── test_password_history.py                           ← Task 4 (Create 4 tests)
    └── test_password_policy_change.py                     ← Task 5 (Create 3 tests)
```

---

## Task 1: Alembic Migration `pwdhist01`

**Files:**
- Create: `ivy-backend/alembic/versions/20260528_pwdhist01_password_history_table.py`

- [ ] **Step 1: Confirm on correct branch + single Alembic head**

```bash
cd ~/Desktop/ivy-backend
git branch --show-current
python3 -c "
from alembic.config import Config
from alembic.script import ScriptDirectory
print('heads:', ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
"
```

Expected:
- branch: `feat/password-policy-strengthening-2026-05-28-backend`
- heads: `('intghealth01',)`（single head）

**若 heads 多於 1 個（user 並行加新 head）**：需先補 merge migration。STOP 並 ask user。

- [ ] **Step 2: Create migration file**

寫入 `~/Desktop/ivy-backend/alembic/versions/20260528_pwdhist01_password_history_table.py`：

```python
"""password_history table for replay prevention.

Revision ID: pwdhist01
Revises: intghealth01
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa


revision = "pwdhist01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_password_history_user_id_created_at",
        "password_history",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_password_history_user_id_created_at", table_name="password_history"
    )
    op.drop_table("password_history")
```

- [ ] **Step 3: Verify Alembic recognizes new head**

```bash
cd ~/Desktop/ivy-backend
python3 -c "
from alembic.config import Config
from alembic.script import ScriptDirectory
print('heads:', ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
"
```

Expected: `heads: ('pwdhist01',)` — single head 不變（從 intghealth01 接上去）。

若仍是 ('intghealth01',) 或顯示多 head 錯誤：file 內容拼錯，回 Step 2 review。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-backend
git add alembic/versions/20260528_pwdhist01_password_history_table.py
git status --short
```

Expected: `A  alembic/versions/20260528_pwdhist01_password_history_table.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(alembic): pwdhist01 建 password_history 表

password_history(id PK, user_id FK CASCADE, password_hash, created_at)
+ index (user_id, created_at DESC) for assert_not_recently_used 查詢。

Parent: intghealth01

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §5.1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: HIBP Helper (TDD)

**Files:**
- Create: `ivy-backend/utils/hibp.py`
- Test: `ivy-backend/tests/test_hibp.py`

- [ ] **Step 1: Write failing tests**

寫入 `~/Desktop/ivy-backend/tests/test_hibp.py`:

```python
"""HIBP k-anonymity assertion tests。"""
from unittest.mock import patch, MagicMock

import pytest

from utils.hibp import PasswordPwnedError, assert_not_pwned


def _fake_response(text: str, status: int = 200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


def test_assert_not_pwned_raises_when_suffix_in_response():
    """SHA1('password') = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
    prefix=5BAA6 / suffix=1E4C9B93F3F0682250B6CF8331B7EE68FD8"""
    response_body = (
        "1E4C9B93F3F0682250B6CF8331B7EE68FD8:9659365\r\n"
        "1E4D7E893E80B79F18CDA72FF6D9CDD68B9:1\r\n"
    )
    with patch("utils.hibp.requests.get", return_value=_fake_response(response_body)):
        with pytest.raises(PasswordPwnedError) as ei:
            assert_not_pwned("password")
        assert ei.value.occurrences == 9659365


def test_assert_not_pwned_passes_when_suffix_not_in_response():
    """random password 不在 HIBP DB。"""
    response_body = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:1\r\n"
    with patch("utils.hibp.requests.get", return_value=_fake_response(response_body)):
        assert_not_pwned("password")


def test_assert_not_pwned_fail_open_on_network_error():
    """network error → fail-open（不 raise）。"""
    import requests as req_mod

    with patch(
        "utils.hibp.requests.get",
        side_effect=req_mod.ConnectionError("network down"),
    ):
        assert_not_pwned("password")  # no raise


def test_assert_not_pwned_fail_open_on_timeout():
    import requests as req_mod

    with patch(
        "utils.hibp.requests.get", side_effect=req_mod.Timeout("slow")
    ):
        assert_not_pwned("password")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_hibp.py -v 2>&1 | tail -10
```

Expected: 4 collection errors — `ModuleNotFoundError: No module named 'utils.hibp'`

- [ ] **Step 3: Create the helper**

寫入 `~/Desktop/ivy-backend/utils/hibp.py`:

```python
"""HIBP (Have I Been Pwned) k-anonymity password check.

api.pwnedpasswords.com 提供 k-anonymity SHA-1 query：
- 將密碼 SHA-1 hash 取前 5 碼當 prefix
- GET https://api.pwnedpasswords.com/range/{prefix}
- 回傳：所有 SHA-1 開頭符合 prefix 的 hash 後 35 碼 + 出現次數
- 我們本機比對 hash 後 35 碼是否在 response 內

優點：HIBP 永不知道完整密碼或 hash；只看到 5-char prefix。

Fail-open: timeout / network error 視為「不在 HIBP DB」，讓 user 完成
密碼設定，避免因 HIBP API down 整站無法改密碼。
"""
from __future__ import annotations

import hashlib
import logging

import requests

logger = logging.getLogger(__name__)

HIBP_API_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 3.0


class PasswordPwnedError(Exception):
    """密碼在 HIBP DB 中（已洩漏）。"""

    def __init__(self, occurrences: int):
        self.occurrences = occurrences
        super().__init__(f"Password found in HIBP DB ({occurrences} occurrences)")


def assert_not_pwned(password: str) -> None:
    """檢查密碼是否在 HIBP DB；命中則 raise PasswordPwnedError。

    Fail-open：network/timeout error 視為 not-pwned 放行（log warning）。
    """
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        resp = requests.get(
            HIBP_API_URL.format(prefix=prefix),
            timeout=HIBP_TIMEOUT_SECONDS,
            headers={"Add-Padding": "true"},
        )
        resp.raise_for_status()
    except (requests.RequestException, requests.Timeout) as e:
        logger.warning("HIBP unreachable, fail-open password set: %s", e)
        return

    for line in resp.text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 2:
            continue
        if parts[0].upper() == suffix:
            try:
                count = int(parts[1])
            except ValueError:
                count = 1
            raise PasswordPwnedError(occurrences=count)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_hibp.py -v 2>&1 | tail -10
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/hibp.py tests/test_hibp.py
git status --short
```

Expected: 2 行 `A`；user WIP 仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
feat(utils): 加入 HIBP k-anonymity assert_not_pwned

api.pwnedpasswords.com 5-char SHA-1 prefix query 不送完整密碼/hash。
Fail-open: timeout/network error 視為 not-pwned 放行，避免 API down
整站無法改密碼。

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: PasswordHistory ORM Class

**Files:**
- Modify: `ivy-backend/models/auth.py`

- [ ] **Step 1: Read 既有 imports + 結構**

```bash
sed -n '1,25p' ~/Desktop/ivy-backend/models/auth.py
```

確認 imports 已有 `Column / Integer / String / DateTime / ForeignKey`（既有 User class 都用）；`text` 可能未 import — 需檢查。

- [ ] **Step 2: Add `text` import (if missing) + PasswordHistory class**

讀 `~/Desktop/ivy-backend/models/auth.py` 確認既有 import 區是否含 `text`。若無，加進既有 `from sqlalchemy import (...)` block：

- old:
```
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    JSON,
)
```
- new:
```
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    JSON,
    text,
)
```

在檔案最末加 PasswordHistory class（讀檔到結尾後 append）：

```python


class PasswordHistory(Base):
    """密碼變更歷史（防重用最近 N 個密碼）。

    Per user 一行為一次密碼變更（含 hash）。assert_not_recently_used
    對最近 PASSWORD_HISTORY_DEPTH 筆 verify_password 比對。
    """

    __tablename__ = "password_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="用戶 ID",
    )
    password_hash = Column(String(255), nullable=False, comment="密碼雜湊")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="變更時間",
    )
```

- [ ] **Step 3: Verify import works**

```bash
cd ~/Desktop/ivy-backend
python3 -c "from models.auth import PasswordHistory; print(PasswordHistory.__tablename__)" && echo "OK"
```

Expected: `password_history` + `OK`

- [ ] **Step 4: 確認 models/__init__.py 集中註冊（若有）**

```bash
cd ~/Desktop/ivy-backend
grep "PasswordHistory\|from models.auth" models/__init__.py 2>&1
```

如果 `models/__init__.py` 集中 export 其他 model（既有 User 等），需在同位置加 `PasswordHistory`（MEMORY 提到 `models/__init__.py` 必須中央 import，否則 Base.metadata.create_all 漏建表）。

若 `models/__init__.py` 有 `from .auth import User, ...` 行：

- 找 `from .auth import` 那一行
- 加 `PasswordHistory` 進該 import list

若 `models/__init__.py` 完全不 import models/auth.py 的東西（每個 caller 各自 import），則此 step 跳過。

```bash
grep "from .auth import\|from models.auth import" models/__init__.py 2>&1 | head -5
```

If 結果有 `from .auth import` 行：Edit 加 PasswordHistory；If 無，skip。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/auth.py
# 若 models/__init__.py 有改:
# git add models/__init__.py
git status --short
```

Expected: `M  models/auth.py`（+ optional `M  models/__init__.py`）。

```bash
git commit -m "$(cat <<'EOF'
feat(models): 加入 PasswordHistory ORM class

對應 alembic pwdhist01 password_history 表。FK CASCADE delete from users。

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §5.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Password History Helper (TDD)

**Files:**
- Create: `ivy-backend/utils/password_history.py`
- Test: `ivy-backend/tests/test_password_history.py`

- [ ] **Step 1: Write failing tests**

寫入 `~/Desktop/ivy-backend/tests/test_password_history.py`:

```python
"""Password history replay prevention tests。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base
from models.auth import PasswordHistory, User
from utils.auth import hash_password
from utils.password_history import (
    PASSWORD_HISTORY_DEPTH,
    assert_not_recently_used,
    record,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ph.sqlite'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    user = User(
        username="alice",
        password_hash=hash_password("Initial-Password-1"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        token_version=0,
    )
    s.add(user)
    s.commit()
    yield s, user
    s.close()


def test_assert_not_recently_used_passes_empty_history(db):
    s, user = db
    assert_not_recently_used(s, user.id, "Brand-New-Password-1")


def test_assert_not_recently_used_raises_on_exact_match(db):
    s, user = db
    h1 = hash_password("Old-Password-One-1")
    record(s, user.id, h1)
    with pytest.raises(HTTPException) as ei:
        assert_not_recently_used(s, user.id, "Old-Password-One-1")
    assert ei.value.status_code == 400


def test_assert_not_recently_used_only_checks_last_N(db):
    """超過 PASSWORD_HISTORY_DEPTH 的舊紀錄不應命中。"""
    s, user = db
    oldest = "Oldest-Password-1234"
    record(s, user.id, hash_password(oldest))
    for i in range(PASSWORD_HISTORY_DEPTH):
        record(s, user.id, hash_password(f"Filler-Password-{i:04d}"))
    # 最舊的應可重用
    assert_not_recently_used(s, user.id, oldest)


def test_assert_not_recently_used_isolates_users(db):
    s, user = db
    h1 = hash_password("User-A-Password-1")
    record(s, user.id, h1)
    # 對另一 user_id 應該不擋
    assert_not_recently_used(s, 9999, "User-A-Password-1")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_password_history.py -v 2>&1 | tail -10
```

Expected: collection error — `ModuleNotFoundError: No module named 'utils.password_history'`

- [ ] **Step 3: Create the helper**

寫入 `~/Desktop/ivy-backend/utils/password_history.py`:

```python
"""Password history replay prevention.

每次密碼變更時 record(user_id, hash)；下次變更前 assert_not_recently_used
比對最近 N 個 hash。N=5（NIST SP 800-63B 建議 5-10）。
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.auth import PasswordHistory
from utils.auth import verify_password

logger = logging.getLogger(__name__)

PASSWORD_HISTORY_DEPTH = 5


def assert_not_recently_used(
    session: Session, user_id: int, new_plaintext_password: str
) -> None:
    """檢查 new_plaintext_password 是否與最近 PASSWORD_HISTORY_DEPTH 個 hash 相符。

    命中則 raise HTTPException(400)。比對方式：對每筆歷史 hash call
    verify_password（含 salt + iterations 解析）。
    """
    rows = (
        session.query(PasswordHistory)
        .filter(PasswordHistory.user_id == user_id)
        .order_by(desc(PasswordHistory.created_at))
        .limit(PASSWORD_HISTORY_DEPTH)
        .all()
    )
    for row in rows:
        if verify_password(new_plaintext_password, row.password_hash):
            raise HTTPException(
                status_code=400,
                detail=f"不可重複使用最近 {PASSWORD_HISTORY_DEPTH} 個密碼",
            )


def record(session: Session, user_id: int, password_hash: str) -> None:
    """記錄一筆密碼變更歷史。caller 應在 user.password_hash = new_hash 後呼叫。"""
    session.add(PasswordHistory(user_id=user_id, password_hash=password_hash))
    session.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_password_history.py -v 2>&1 | tail -10
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/password_history.py tests/test_password_history.py
git status --short
```

Expected: 2 行 `A`。

```bash
git commit -m "$(cat <<'EOF'
feat(utils): 加入 password_history helper

assert_not_recently_used 對最近 PASSWORD_HISTORY_DEPTH=5 個 hash
verify_password 比對；命中 HTTPException(400)。record 寫一筆歷史
（caller 在 user.password_hash = new_hash 後呼叫）。

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §5.3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 密碼長度 8 → 12 + 接 HIBP

**Files:**
- Modify: `ivy-backend/utils/auth.py` (line 105 + 108-130)
- Test: `ivy-backend/tests/test_password_policy_change.py` (new)

- [ ] **Step 1: Write failing tests**

寫入 `~/Desktop/ivy-backend/tests/test_password_policy_change.py`:

```python
"""驗證 _PASSWORD_MIN_LENGTH 改為 12 + HIBP 整合。"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from utils.auth import validate_password_strength


def _stub_hibp_no_match():
    """HIBP response 不命中（空 suffix list）。"""
    m = patch("utils.hibp.requests.get")
    gh = m.start()
    gh.return_value.text = ""
    gh.return_value.status_code = 200
    gh.return_value.raise_for_status = lambda: None
    return m


def test_validates_12_char_minimum():
    """11 字元應該擋。"""
    m = _stub_hibp_no_match()
    try:
        with pytest.raises(HTTPException) as ei:
            validate_password_strength("ShortPwd123")  # 11 chars
        assert ei.value.status_code == 400
        assert "12" in ei.value.detail
    finally:
        m.stop()


def test_passes_at_12_chars_with_complexity():
    m = _stub_hibp_no_match()
    try:
        validate_password_strength("LongerPwd123")  # 12 chars, ABC abc 123
    finally:
        m.stop()


def test_rejects_pwned_password():
    """HIBP 命中應拒。"""
    import hashlib

    sha1 = hashlib.sha1(b"Test-Pwned-Password-1").hexdigest().upper()
    suffix = sha1[5:]
    response_body = f"{suffix}:42\r\n"
    with patch("utils.hibp.requests.get") as gh:
        gh.return_value.text = response_body
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        with pytest.raises(HTTPException) as ei:
            validate_password_strength("Test-Pwned-Password-1")
        assert "外洩" in ei.value.detail
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_password_policy_change.py -v 2>&1 | tail -10
```

Expected: 3 fails — 既有 `validate_password_strength` min=8 + 無 HIBP，所以：
- test_validates_12_char_minimum: 11-char 通過 → AssertionError (no raise)
- test_passes_at_12_chars_with_complexity: 已通過（既有 8 也通過）— 可能 PASS（但不打緊；本 test 主要驗 not raise）
- test_rejects_pwned_password: 未 raise HIBP error → AssertionError

- [ ] **Step 3: Modify utils/auth.py**

讀 `~/Desktop/ivy-backend/utils/auth.py:100-135` 確認 `_PASSWORD_MIN_LENGTH` + `validate_password_strength()` 結構。

Use Edit on `~/Desktop/ivy-backend/utils/auth.py`：

第一處：min length。
- old: `_PASSWORD_MIN_LENGTH = 8`
- new: `_PASSWORD_MIN_LENGTH = 12`

第二處：在 `validate_password_strength()` body 末尾（既有 `if errors: raise HTTPException(...)` 後）加 HIBP check。

- old:
```python
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"密碼強度不足：{', '.join(errors)}",
        )


def hash_password(password: str) -> str:
```
- new:
```python
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"密碼強度不足：{', '.join(errors)}",
        )

    # HIBP check（fail-open: network error 放行）
    from utils.hibp import PasswordPwnedError, assert_not_pwned

    try:
        assert_not_pwned(password)
    except PasswordPwnedError as e:
        raise HTTPException(
            status_code=400,
            detail=f"此密碼已在資料外洩名單中（出現 {e.occurrences} 次），請選用其他密碼",
        )


def hash_password(password: str) -> str:
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_password_policy_change.py -v 2>&1 | tail -10
```

Expected: 3 passed

- [ ] **Step 5: Run existing auth tests for regression**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_jwt_blocklist.py tests/test_password_*.py tests/test_auth*.py --tb=no -q 2>&1 | tail -10
```

Expected: 全 pass。**注意**：既有 `test_password_*.py` 內若有「password=8 chars 通過」之類 test，會因 12 限制 fail；需找 + 改密碼到 12+ chars。先跑看哪些 fail，然後改。預期可能 fail 的範圍：
- `tests/test_auth*.py` 對 register/change_password 用 8-char fixture（如 `"Pass1234"`）

若 fail 數較多，是 expected behavior change；改 fixture 密碼到 ≥12 chars 即可。

⚠️ **若 regression 超過 10 個 fixture 密碼要改**，停下 ask user 確認是否該 plan 改路徑（例如先發佈 12-char、留 HIBP 給下個 PR）。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/auth.py tests/test_password_policy_change.py
# 若 step 5 改了既有 test fixture:
# git add tests/test_<modified>.py ...
git status --short
```

```bash
git commit -m "$(cat <<'EOF'
feat(auth): 密碼長度 8→12 + 接 HIBP k-anonymity check

_PASSWORD_MIN_LENGTH 8→12 (NIST SP 800-63B)；validate_password_strength
末加 HIBP assert_not_pwned，命中拋 400「外洩名單」訊息。Fail-open:
HIBP unreachable 時放行。

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §3 §6.1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 3 個 Endpoint 接 password_history

**Files:**
- Modify: `ivy-backend/api/auth.py` (3 endpoints: change/create/reset password + import)

- [ ] **Step 1: Add import to api/auth.py**

讀 `~/Desktop/ivy-backend/api/auth.py:1-50` 找既有 `from utils.X import ...` block。

Use Edit on `~/Desktop/ivy-backend/api/auth.py`：

找既有 `from utils.auth import ...` 後，加：
```python
from utils.password_history import (
    assert_not_recently_used,
    record as record_password_history,
)
```

具體位置 implementer Read 後決定（既有 utils import 區末尾即可）。

- [ ] **Step 2: Modify change_password (line ~1041)**

讀 `~/Desktop/ivy-backend/api/auth.py:1038-1067` 確認 endpoint structure。

Use Edit on `~/Desktop/ivy-backend/api/auth.py`：

- old:
```python
        if not verify_password(data.old_password, user.password_hash):
            _record_pwd_change_failure(user_id)  # 記失敗 → 累積觸發 lockout
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
```
- new:
```python
        if not verify_password(data.old_password, user.password_hash):
            _record_pwd_change_failure(user_id)  # 記失敗 → 累積觸發 lockout
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        validate_password_strength(data.new_password)
        assert_not_recently_used(session, user.id, data.new_password)
        new_hash = hash_password(data.new_password)
        user.password_hash = new_hash
        record_password_history(session, user.id, new_hash)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
```

- [ ] **Step 3: Modify create_user (line ~1153)**

讀 `~/Desktop/ivy-backend/api/auth.py:1150-1170` 確認 structure。`create_user` 在 new user 場景無 history 可比對，**只 record** 不 assert。

Use Edit on `~/Desktop/ivy-backend/api/auth.py`：

- old:
```python
        # 驗證密碼強度
        validate_password_strength(data.password)

        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=hash_password(data.password),
            role=data.role,
            permission_names=final_permission_names,
            must_change_password=True,  # 新帳號強制首次登入修改密碼
        )
        session.add(user)
        session.commit()
        return {"message": "帳號建立成功", "id": user.id}
```
- new:
```python
        # 驗證密碼強度
        validate_password_strength(data.password)

        new_hash = hash_password(data.password)
        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=new_hash,
            role=data.role,
            permission_names=final_permission_names,
            must_change_password=True,  # 新帳號強制首次登入修改密碼
        )
        session.add(user)
        session.flush()  # 取 user.id 給 password_history FK
        record_password_history(session, user.id, new_hash)
        session.commit()
        return {"message": "帳號建立成功", "id": user.id}
```

- [ ] **Step 4: Modify reset_password (line ~1209)**

讀 `~/Desktop/ivy-backend/api/auth.py:1200-1220` 確認 structure。

Use Edit on `~/Desktop/ivy-backend/api/auth.py`：

- old:
```python
        _assert_can_manage_user(current_user, session=session, target_user=user)

        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = True  # 管理員代為重設密碼，強制當事人下次登入修改
        user.token_version = (
            user.token_version or 0
        ) + 1  # 使所有現有 session 的 token 立即無法刷新
        session.commit()
        return {"message": "密碼重設成功"}
```
- new:
```python
        _assert_can_manage_user(current_user, session=session, target_user=user)

        validate_password_strength(data.new_password)
        assert_not_recently_used(session, user.id, data.new_password)
        new_hash = hash_password(data.new_password)
        user.password_hash = new_hash
        record_password_history(session, user.id, new_hash)
        user.must_change_password = True  # 管理員代為重設密碼，強制當事人下次登入修改
        user.token_version = (
            user.token_version or 0
        ) + 1  # 使所有現有 session 的 token 立即無法刷新
        session.commit()
        return {"message": "密碼重設成功"}
```

- [ ] **Step 5: Verify import + run regression tests**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import api.auth" && echo "import OK"
python3 -m pytest tests/test_auth*.py tests/test_jwt_blocklist.py --tb=short -q 2>&1 | tail -20
```

Expected: import OK + 全 pass。
- 若有 fail 多半因 既有 fixture 用 8-char 密碼（Task 5 step 5 應該已涵蓋；若 Task 6 又出新 fail，調 fixture 密碼到 ≥12 chars）

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/auth.py
# 若需改既有 fixture:
# git add tests/test_<modified>.py
git status --short
```

```bash
git commit -m "$(cat <<'EOF'
feat(auth): 3 個密碼變更 endpoint 接 password_history

change_password / reset_password 加 assert_not_recently_used；
3 個 endpoint 都加 record_password_history。create_user 需在
session.flush() 後拿 user.id 才能 record FK。

Refs: docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md §6.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 全套 pytest 跑綠

**Files:** 無修改

- [ ] **Step 1: Run full pytest suite**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest --tb=no -q 2>&1 | tail -10
```

Expected: 5576+ passed + 本 PR 新加 11 tests (4 hibp + 4 password_history + 3 policy)。

預期 regression：**可能有既有 test 用 8-char password fixture**（如 `"Pass1234"`），需改成 ≥12 chars。本 PR baseline 是 origin/main，與 sub-project #3 的 5576 不同（無 #1-3 影響）。

預期 fail mode：
- Fixture 密碼 < 12 chars → `validate_password_strength` 400「至少 12 個字元」
- 既有 test 預期 `create_user` 用 8-char 密碼成功 → 改 fixture
- HIBP unmocked outbound 網路 → 既有 test 命中 production password DB 可能誤 raise PasswordPwnedError；test 環境需 mock HIBP（建議在 conftest autouse fixture mock `utils.hibp.requests.get` 回空 response）

⚠️ **HIBP outbound network risk**：既有 test 若實際打 HIBP API，CI 環境網路存在但會增延遲且不穩定。建議加 conftest fixture 自動 mock HIBP 預設「不命中」：

如果 step 1 fail 主因是 HIBP 連線，補一個 conftest fixture（per-package 或 global）：

寫入 `~/Desktop/ivy-backend/tests/conftest.py` 末尾（若已存在 conftest.py 在 root tests/ 下；否則建在 ivy-backend/conftest.py）：

```python
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_hibp_default_no_match(request):
    """test 預設 mock HIBP 回「不命中」，避免每個 test 都打外部 API。

    Test 想驗 HIBP 行為時可顯式 patch utils.hibp.requests.get 覆寫。
    """
    if "test_hibp" in request.node.nodeid:
        # test_hibp.py 自己 patch，這裡 yield 不擋
        yield
        return

    fake_resp = MagicMock()
    fake_resp.text = ""
    fake_resp.status_code = 200
    fake_resp.raise_for_status = lambda: None
    with patch("utils.hibp.requests.get", return_value=fake_resp):
        yield
```

讀 `~/Desktop/ivy-backend/tests/conftest.py` 確認是否已有 autouse fixture / 命名衝突 → 視需要 append。

實際上有可能 conftest 已 mock 外部 requests；先看 step 1 結果再決定要不要加。

- [ ] **Step 2: 對 fail 列表 triage**

針對 step 1 fail：
- 計算 fail 數
- 若 fail 全是「fixture 密碼 < 12 chars」，逐一改 fixture 密碼（如 `"Pass1234"` → `"LongPass1234"`）
- 若 fail 是 HIBP 網路問題，加 conftest mock

逐一 fix，re-run 直到全綠。

- [ ] **Step 3: Final regression check**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest --tb=no -q 2>&1 | tail -5
```

Expected: 全綠（含 5576+ baseline + 11 new tests）。

- [ ] **Step 4: Diff stat**

```bash
cd ~/Desktop/ivy-backend
git diff --stat origin/main..HEAD
git log --oneline origin/main..HEAD
```

Expected: 約 6 個 commit（spec + plan + Task 1-6）+ 8-10 個檔案改動。

---

## Task 8: Push & Open PR

**Files:** 無修改

- [ ] **Step 1: Push branch**

```bash
cd ~/Desktop/ivy-backend
git push -u origin feat/password-policy-strengthening-2026-05-28-backend 2>&1
```

- [ ] **Step 2: Create PR**

```bash
cd ~/Desktop/ivy-backend
gh pr create --title "feat(security): 密碼政策強化 ≥12 + HIBP + password_history" --body "$(cat <<'EOF'
## Summary
- **\`utils/auth.py\`**：\`_PASSWORD_MIN_LENGTH\` 8 → 12（NIST SP 800-63B）
- **\`utils/hibp.py\`** (new)：HIBP k-anonymity SHA-1 prefix query；fail-open on network error
- **\`utils/password_history.py\`** (new) + Alembic \`pwdhist01\`：password_history 表（depth=5）防重用最近密碼
- **3 個 endpoint 接入** (\`change_password\` / \`create_user\` / \`reset_password\`)：validate + history check + record

## Behavior change
- 既有 user 用 8-char 密碼**仍能登入**（policy 只在 set 時 check）
- 下次設密碼必須 ≥12 chars + 不在 HIBP DB + 不在最近 5 個 hash
- HIBP API unreachable 時 fail-open（log warning + Sentry capture 等 sub-project #2 merge）
- Admin reset_password 改為被 target user 最近 5 個密碼擋

## Rollback
- 完整 revert：revert PR + \`alembic downgrade -1\`
- 只關 HIBP：\`utils/auth.py\` 註解 \`from utils.hibp import...\` block
- 只關 history：\`api/auth.py\` 3 處註解 \`assert_not_recently_used\` + \`record_password_history\`
- 臨時降回 8 chars：\`_PASSWORD_MIN_LENGTH = 8\` 一行改

## Test plan
- [ ] CI 全綠（含 11 new tests）
- [ ] Merge 後 prod 跑 \`alembic upgrade heads\`（建 \`password_history\` 表）
- [ ] 手動煙霧測：change password 給 11-char → 400；給 12-char 但 HIBP 命中 (e.g. \`password1234\`) → 400「外洩名單」；給 12-char OK password → 成功；再 change 回原密碼 → 400「不可重複」

## Out of scope (follow-up per spec §10)
- 前端密碼修改頁 hint 顯示「12 字元」
- 登入時 nag「密碼太短」
- password_history GC scheduler
- HIBP env disable flag

Spec: \`docs/superpowers/specs/2026-05-28-password-policy-strengthening-design.md\`
Plan: \`docs/superpowers/plans/2026-05-28-password-policy-strengthening.md\`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1
```

Expected: PR URL printed。

- [ ] **Step 3: 等 CI 全綠（optional）**

```bash
gh pr checks <PR_NUM> --watch
```

---

## Self-Review

**1. Spec coverage:**
- §3 長度 8→12 → Task 5 ✓
- §4 HIBP → Task 2 + Task 5 (接入) ✓
- §5.1 Alembic migration → Task 1 ✓
- §5.2 ORM class → Task 3 ✓
- §5.3 helper → Task 4 ✓
- §6 接入 → Task 5 (HIBP) + Task 6 (history) ✓
- §7 測試 → 3 個新 test 檔（Task 2 + 4 + 5） + Task 7 既有 regression check ✓
- §8 行為變更 → PR body ✓
- §10 follow-up → PR body Out-of-scope ✓
- §11 風險回退 → PR body Rollback ✓

**2. Placeholder scan:**
- Task 3 Step 4「若 models/__init__.py 集中 export 其他 model」是 conditional check, 含 `grep` 指令引導決策，非 placeholder
- Task 5 Step 5 ⚠️ + Task 7 Step 1 ⚠️ 都是 contingent guidance（fixture 密碼可能需改），含具體 fix path
- 所有 code block 完整，所有 commit message 完整

**3. Type consistency:**
- `assert_not_pwned(password)` signature Task 2 ↔ Task 5 call ✓
- `PasswordPwnedError(occurrences=int)` Task 2 ↔ Task 5 caller `e.occurrences` ✓
- `PasswordHistory` ORM Task 3 ↔ Task 4 query ✓
- `assert_not_recently_used(session, user_id, new_plaintext_password)` Task 4 ↔ Task 6 call (positional) ✓
- `record(session, user_id, password_hash)` Task 4 ↔ Task 6 call `record_password_history(...)` (alias) ✓
- `PASSWORD_HISTORY_DEPTH = 5` Task 4 ↔ Task 4 test 用 ✓
- Alembic revision id `"pwdhist01"` Task 1 ↔ spec §5.1 ✓
- migration parent `intghealth01` Task 1 ↔ spec §10.4 ✓
