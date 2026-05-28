# 密碼政策強化 ≥12 + HIBP + password_history

**日期**：2026-05-28
**狀態**：Design（pending review）
**Scope**：ivy-backend
**前置任務**：P2 audit finding 20-b (密碼政策不足：8 chars + 無 HIBP + 無 history)
**工時估**：1-2 天

---

## 1. 動機

當前密碼政策（`utils/auth.py:104-130`）：
- 最少 8 字元
- 必含大寫 + 小寫 + 數字（無特殊字元）
- **無 HIBP 比對** — user 設 `Password123` 通過但已洩漏億次
- **無 password_history** — user 改密碼可立即改回原來的

威脅：
- 弱密碼通過 strength check → credential stuffing 攻擊成功
- 重複用舊密碼 → 強制變更政策（如月度）失去意義

### 1.1 不做什麼（YAGNI）

- **不加特殊字元要求**：NIST SP 800-63B 已不建議強制特殊字元（用 length + HIBP 替代）；audit 也沒提
- **不強制既有 user 重設密碼**：本 PR 只擋下次 set 動作；既有 8-char + non-HIBP-pwned 密碼仍能登入
- **不加 HIBP env disable flag**：YAGNI；prod fail-open 已 cover offline 情境（dev/test 環境 mock 即可）
- **不在登入時 nag「密碼太短請更新」**：屬獨立 UX feature；本 PR 不做
- **不換 hash 演算法**：既有 PBKDF2-HMAC-SHA256 仍 industry-acceptable

---

## 2. 範圍與整體架構

```
ivy-backend/
├── utils/
│   ├── auth.py                      ← 修改 _PASSWORD_MIN_LENGTH 8→12
│   ├── hibp.py                      ← 新檔：assert_not_pwned k-anonymity check
│   └── password_history.py          ← 新檔：record + assert_not_recently_used
├── models/
│   └── auth.py                      ← 修改：加 PasswordHistory ORM class
├── alembic/versions/
│   └── 20260528_pwdhist01_password_history_table.py    ← 新檔：建 password_history 表
├── api/
│   └── auth.py                      ← 修改 3 處 (change/create/reset password)
└── tests/
    ├── test_hibp.py                 ← 新檔
    ├── test_password_history.py     ← 新檔
    └── test_password_policy_change.py  ← 新檔（既有 password policy test 擴充）
```

---

## 3. 密碼長度 8 → 12

**`utils/auth.py:105`：**

```python
# 修改前
_PASSWORD_MIN_LENGTH = 8

# 修改後
_PASSWORD_MIN_LENGTH = 12
```

`validate_password_strength()` 內 error message 自動沿用變數，無其他改動。

---

## 4. HIBP k-anonymity check

### 4.1 新檔 `utils/hibp.py`

```python
"""HIBP (Have I Been Pwned) k-anonymity password check.

api.pwnedpasswords.com 提供 k-anonymity SHA-1 query：
- 將密碼 SHA-1 hash 取前 5 碼當 prefix
- GET https://api.pwnedpasswords.com/range/{prefix}
- 回傳：所有 SHA-1 開頭符合 prefix 的 hash 後 35 碼 + 出現次數
- 我們本機比對 hash 後 35 碼是否在 response 內

優點：HIBP 永不知道完整密碼或 hash；只看到 5-char prefix。

Fail-open: timeout / network error 視為「不在 HIBP DB」，讓 user 完成
密碼設定，避免因 HIBP API down 整站無法改密碼。失敗事件透過
capture_fail_open helper（sub-project #2 merge 後生效）送 Sentry。
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
            headers={"Add-Padding": "true"},  # HIBP padding 防 traffic analysis
        )
        resp.raise_for_status()
    except (requests.RequestException, requests.Timeout) as e:
        # Fail-open: log + sub-project #2 merge 後可改用 capture_fail_open
        logger.warning(
            "HIBP unreachable, fail-open password set: %s", e
        )
        return

    for line in resp.text.splitlines():
        # 格式：<suffix>:<count>
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

### 4.2 設計理由

- **k-anonymity 不送完整 hash**：用 5-char prefix 守隱私（HIBP 收到的只是 5-char prefix，無法回推 user）
- **`Add-Padding: true`**：HIBP 對 response 加 random padding 防 traffic analysis 推測 suffix
- **timeout 3s**：HIBP API 通常 <500ms 回應，3s 是寬鬆 fallback
- **fail-open**：avoid 密碼設定整站 down；攻擊面 = HIBP outage 期間 user 可設弱密碼，acceptable trade-off
- **沿用 requests**：既有 backend 已用 requests（requirements.txt 含），不增依賴

---

## 5. Password History

### 5.1 新表 `password_history`

**Alembic migration `20260528_pwdhist01_password_history_table.py`：**

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
    op.drop_index("ix_password_history_user_id_created_at", table_name="password_history")
    op.drop_table("password_history")
```

### 5.2 新 ORM class `models/auth.py`

加在既有 User class 後：

```python
class PasswordHistory(Base):
    __tablename__ = "password_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    password_hash = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
```

需 ensure `from sqlalchemy import text` 已在 imports（既有 model 通常有）。

### 5.3 新 helper `utils/password_history.py`

```python
"""Password history replay prevention.

每次密碼變更時 record(user_id, hash)；下次變更前 assert_not_recently_used
比對最近 N 個 hash。N=5（NIST SP 800-63B 建議 5-10）。
"""
from __future__ import annotations

import logging
from typing import Optional

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
    verify_password（含 salt + iterations 解析），避免 hash format 不一致漏抓。
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
    """記錄一筆密碼變更歷史。caller 應在 user.password_hash = new_hash 後呼叫。

    GC：超過 PASSWORD_HISTORY_DEPTH 的舊紀錄不自動刪除（assert_not_recently_used
    只 LIMIT N 筆查詢，舊紀錄不影響行為）。若日後 storage 真的爆，加 scheduler
    每月清；目前 YAGNI（per user 一年 12 次改密碼 = 12 row，可忽略）。
    """
    session.add(PasswordHistory(user_id=user_id, password_hash=password_hash))
    session.flush()
```

### 5.4 設計理由

- **`verify_password` 比對而非 hash 直接比**：既有 `hash_password` 用 PBKDF2 + 隨機 salt，同密碼每次 hash 不同；必須走 `verify_password(plain, stored)` 解析 salt 比對
- **N=5**：NIST SP 800-63B 建議；audit 也指定
- **無 GC scheduler**：YAGNI；正常 user 一年 12 次 = 12 row × 100 users = 1.2K row/year 可忽略
- **caller 在 flush 後**：避免 session race；ORM 預設 flush at commit

---

## 6. 接入 `validate_password_strength()` + 3 處 call site

### 6.1 擴 `validate_password_strength()` signature 加 HIBP

**`utils/auth.py:108`：** signature 不改（仍 `validate_password_strength(password)`），但 body 加 HIBP check：

```python
def validate_password_strength(password: str) -> None:
    """驗證密碼強度。

    規則：
    - 至少 12 字元
    - 至少包含一個大寫字母
    - 至少包含一個小寫字母
    - 至少包含一個數字
    - 不在 HIBP DB（fail-open: HIBP unreachable 時放行）
    """
    errors = []
    if len(password) < _PASSWORD_MIN_LENGTH:
        errors.append(f"至少 {_PASSWORD_MIN_LENGTH} 個字元")
    if not re.search(r"[A-Z]", password):
        errors.append("至少一個大寫英文字母")
    if not re.search(r"[a-z]", password):
        errors.append("至少一個小寫英文字母")
    if not re.search(r"\d", password):
        errors.append("至少一個數字")
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
```

### 6.2 接入 password_history（3 處 call site）

**`api/auth.py` 既有 import 區加：**

```python
from utils.password_history import (
    assert_not_recently_used,
    record as record_password_history,
)
```

**`api/auth.py:1041` `change_password`：**

```python
# 既有
validate_password_strength(data.new_password)
# ... verify_password(old) + check ...

# 新加：history check
assert_not_recently_used(session, user.id, data.new_password)

# 既有
new_hash = hash_password(data.new_password)
user.password_hash = new_hash

# 新加：record
record_password_history(session, user.id, new_hash)

# 既有 commit
session.commit()
```

**`api/auth.py:1153` `create_user`：**

```python
# 既有
validate_password_strength(data.password)

# create_user 無 history check（新帳號無 history）
# 但仍 record 第一筆 history（避免 admin 重設後 user 改回同密碼）
new_hash = hash_password(data.password)
user.password_hash = new_hash

# 新加：record
record_password_history(session, user.id, new_hash)
```

**`api/auth.py:1209` `reset_password`：**

```python
# 既有 admin reset for target user
validate_password_strength(data.new_password)

# 加：assert_not_recently_used for target user（防 admin 改回原密碼）
assert_not_recently_used(session, target_user.id, data.new_password)

# 既有
new_hash = hash_password(data.new_password)
target_user.password_hash = new_hash

# 新加：record
record_password_history(session, target_user.id, new_hash)
```

---

## 7. 測試

### 7.1 `tests/test_hibp.py`

```python
"""HIBP k-anonymity assertion tests。"""
from unittest.mock import patch, MagicMock

import pytest

from utils.hibp import HIBP_API_URL, PasswordPwnedError, assert_not_pwned


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
        "1E4C9B93F3F0682250B6CF8331B7EE68FD8:9659365\r\n"  # matches!
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
        # 不 raise 即視為 pass
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

### 7.2 `tests/test_password_history.py`

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
    # record N+1 個 password，最舊的應該不在 check 範圍
    oldest = "Oldest-Password-1234"
    record(s, user.id, hash_password(oldest))
    for i in range(PASSWORD_HISTORY_DEPTH):
        record(s, user.id, hash_password(f"Filler-Password-{i:04d}"))
    # 應該可以重用最舊的
    assert_not_recently_used(s, user.id, oldest)


def test_assert_not_recently_used_isolates_users(db):
    s, user = db
    h1 = hash_password("User-A-Password-1")
    record(s, user.id, h1)
    # 對另一 user_id 應該不擋
    assert_not_recently_used(s, 9999, "User-A-Password-1")
```

### 7.3 `tests/test_password_policy_change.py`

```python
"""驗證 _PASSWORD_MIN_LENGTH 改為 12 + HIBP 整合。"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from utils.auth import validate_password_strength


def test_validates_12_char_minimum():
    """11 字元應該擋。"""
    with patch("utils.hibp.requests.get") as gh:
        gh.return_value.text = ""
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        with pytest.raises(HTTPException) as ei:
            validate_password_strength("ShortPwd123")  # 11 chars
        assert ei.value.status_code == 400
        assert "12" in ei.value.detail


def test_passes_at_12_chars_with_complexity():
    with patch("utils.hibp.requests.get") as gh:
        gh.return_value.text = ""
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        validate_password_strength("LongerPwd123")  # 12 chars, ABC abc 123


def test_rejects_pwned_password():
    """HIBP 命中應拒。"""
    # 構造 HIBP response 命中
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

---

## 8. 行為變更與 User 影響

| 場景 | 既有 | 新 |
|---|---|---|
| 既有 user 用 8-char 密碼登入 | OK | OK（不擋登入；只擋下次 set） |
| User 改密碼設 `Password1` (9 chars) | OK | 400 「至少 12 個字元」 |
| User 改密碼設 `LongerPwd123` (12 chars, 但 HIBP 命中) | OK | 400 「資料外洩名單」 |
| User 改密碼設與最近 5 個相同 | OK | 400 「不可重複使用最近 5 個密碼」 |
| Admin reset_password 給 user 設與該 user 最近 5 個相同 | OK | 400 |
| HIBP API offline | n/a | fail-open + Sentry warning |

**前端 UX 影響：** 既有密碼修改頁需顯示「至少 12 字元」提示。建議在前端 input hint 顯示完整規則。**本 PR 不改前端**（屬獨立 sub-project；前端可分開接 follow-up）。

---

## 9. Prerequisites

無 user 手動操作。Alembic migration 在 PR merge 後 prod `alembic upgrade heads` 即可。

---

## 10. Out of Scope（follow-up）

| Follow-up | 屬性 | 何時做 |
|---|---|---|
| 前端密碼修改頁 hint 改顯示「12 字元」 | UX | 本 PR merge 後 sprint |
| 登入時提示「密碼太短，建議更新」nag | UX | 若 prod 強制 user 更新需求出現 |
| password_history GC scheduler | storage | per user 一年 12 row 太少；YAGNI |
| HIBP env disable flag | 彈性 | YAGNI；test 已 mock |
| 換 hash 演算法（Argon2） | 強度 | 既有 PBKDF2 仍 acceptable |

---

## 11. 風險與回退

### 11.1 主要風險

- **HIBP API 限制率**：HIBP 對單 IP 無限制；但密碼 set 並非高頻 → low risk
- **HIBP response parsing 漏邊界**：mitigation 是 §7.1 unit test 4 條 cover normal/match/network-fail/timeout
- **password_history.verify_password 對舊 hash format 失效**：既有 `verify_password()` 已 fallback 兼容（utils/auth.py:152 既有測試 cover）
- **Alembic single head 變多 head**：mitigation 是 spec §5.1 明確 `down_revision = "intghealth01"`（目前 single head），plan task 開始前需確認仍 single head；若 user 並行加 head 需先 alembic merge

### 11.2 回退方式

- **完整回退**：revert PR + `alembic downgrade -1`（password_history 表 drop）
- **只關 HIBP**：`utils/auth.py` validate_password_strength body 註解 `from utils.hibp import...` block
- **只關 history**：`api/auth.py` 3 處註解 `assert_not_recently_used` + `record_password_history`
- **臨時降回 8 chars**：`_PASSWORD_MIN_LENGTH = 8` 一行改

---

## 12. 預估與分工

- **規模**：3 個新檔 (`utils/hibp.py` ~70 行 / `utils/password_history.py` ~50 行 / migration ~30 行) + 4 修改檔（utils/auth.py / models/auth.py / api/auth.py 3 處 / 既有 test files 無動）+ 3 新 test 檔
- **工時**：1-2 天（含 spec / plan / commit / push / PR）
- **PR 數**：1（backend only）
- **依賴**：無（與 sub-project #1-3 獨立）；fail-open observability 在 #2 merge 後自動繼承
- **block 後續 sub-project？** 不 block
