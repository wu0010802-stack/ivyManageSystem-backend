# 兒童醫療欄位 Application-Level 加密 + 取用稽核（P0d）

**日期**: 2026-05-28
**範圍**: ivy-backend
**Sprint**: P0d（4 個 P0 法規/個資 sprint 中的最後一個，技術門檻最高）
**預估**: ~2 工作週

---

## 1. 背景與動機

兒童醫療資訊明文存、無 application-level 加密，DB dump 即洩漏。

**現況證據：**
- `models/classroom.py:174-177` `Student.allergy / medication / special_needs` Text 明文
- `models/portfolio.py:340-341` `StudentObservation.height_cm / weight_kg` 明文
- `models/contact_book.py:68` `StudentContactBookEntry.temperature_c` 每日量測明文
- Sentry denylist 已含這些欄位但 audit log（P0b 處理）、log、權限僅讀寫 gate 無「特種個資取用獨立同意 + 額外稽核」

**違反**：個資法 §6（特種個資——醫療、健康檢查）「需法律明文 / 當事人書面同意」；§47 罰責 5-50 萬（比一般個資加重 2.5 倍）。

---

## 2. 目標與非目標

### 目標
1. **加密**：app symmetric key 加密 `allergy / medication / special_needs / temperature_c`（高敏感）；`height_cm / weight_kg` 列為 deferred（成長量測敏感度較低，加密影響查詢效能太大）
2. **取用稽核**：`medical_access_log` 表記錄誰、何時、為何讀取醫療欄位（不與 audit_log 混）
3. **Reason 欄位**：教師端讀「過敏/用藥」endpoint 強制要求 `reason` query param（仿 BREAKING bonus reason 流程）
4. **依賴 P0c 完成**：取用 reason 需 audit-trail 化操作 pattern，consent log 需確認家長同意 `medical_processing` scope

### 非目標
1. **既有 plaintext 資料 retroactive encrypt**：v1 PR 提供 backfill script，但執行時機交 ops 安排（線上 maintenance window）
2. **TDE (Transparent Data Encryption)**：DB 層加密由 Supabase 既有 at-rest encryption 已覆蓋，application-level 加密是額外防線（DBA / dump leak / SQL injection 防護）
3. **height/weight 加密**：成長量測值需要 range query（如「查看本季 BMI 異常學生」），加密後 query 不可行。列為 follow-up，可改用 deterministic encryption + functional index
4. **多 key versioning / rotation**：v1 用單一 master key，rotation 列為 follow-up

---

## 3. 設計

### 3.1 加密 Helper `utils/medical_encryption.py`

```python
"""utils/medical_encryption.py — application-level 對稱加密。

Backend: cryptography.Fernet (AES-128-CBC + HMAC-SHA256)
Key: 從 env `MEDICAL_FIELD_ENCRYPTION_KEY`（base64 32 bytes）
"""
from cryptography.fernet import Fernet, InvalidToken
from config import get_settings

_FERNET: Fernet | None = None

def _get_fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        key = get_settings().medical.encryption_key
        if not key:
            raise RuntimeError("MEDICAL_FIELD_ENCRYPTION_KEY not set")
        _FERNET = Fernet(key.encode())
    return _FERNET

def encrypt_medical(plaintext: str | None) -> str | None:
    """加密；None / empty 直接回傳。輸出 base64 string (ASCII safe)."""
    if plaintext is None or plaintext == "":
        return plaintext
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")

def decrypt_medical(ciphertext: str | None) -> str | None:
    """解密；輸入若不是 valid Fernet token（legacy plaintext）原樣回傳（migration window）"""
    if ciphertext is None or ciphertext == "":
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ciphertext  # legacy plaintext during migration
```

### 3.2 SQLAlchemy TypeDecorator `utils/medical_field_type.py`

```python
"""透明加解密：ORM 層 column 自動處理。"""
from sqlalchemy.types import TypeDecorator, Text
from utils.medical_encryption import encrypt_medical, decrypt_medical

class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_medical(value)

    def process_result_value(self, value, dialect):
        return decrypt_medical(value)
```

### 3.3 Model 改動

```python
# models/classroom.py
from utils.medical_field_type import EncryptedText

class Student(Base):
    ...
    allergy = Column(EncryptedText, nullable=True, comment="過敏原（加密）")
    medication = Column(EncryptedText, nullable=True, comment="用藥說明（加密）")
    special_needs = Column(EncryptedText, nullable=True, comment="特殊需求（加密）")

# models/contact_book.py
class StudentContactBookEntry(Base):
    ...
    temperature_c = Column(EncryptedText, nullable=True, comment="體溫（加密）")
    # 注意：原本是 Numeric，改 EncryptedText 後存的是字串；caller 需轉 float
```

**注意 trade-off**：temperature_c 原本 Numeric，改 EncryptedText 後失去 query 能力（如「找體溫 >38 的學生」），但業務上似乎沒這類 query，可接受。**先 grep 確認**，如有則拆 follow-up。

### 3.4 Migration

```python
# alembic/versions/medic01_encrypt_medical_fields.py
"""加密 medical fields"""

def upgrade():
    # 改 column 型別：Text → EncryptedText（DB 端仍是 Text，僅 ORM 層加解密）
    # 加 medical_access_log 表
    op.create_table(
        "medical_access_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("student_id", sa.Integer, sa.ForeignKey("students.id"), nullable=False, index=True),
        sa.Column("field_name", sa.String(50), nullable=False),  # allergy / medication / special_needs / temperature_c
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("accessed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ip_address", sa.String(45), nullable=True),
    )
    op.create_index("ix_mal_student_field_time", "medical_access_log", ["student_id", "field_name", "accessed_at"])

def downgrade():
    op.drop_table("medical_access_log")
```

### 3.5 Endpoint 改動：強制 reason

```python
# api/students.py
@router.get("/{student_id}/medical")
def get_student_medical(
    student_id: int,
    reason: str = Query(..., min_length=10, description="讀取醫療資訊原因（≥10 字）"),
    current_user: User = Depends(...),
    session = Depends(get_session),
):
    require_permission(current_user, Permission.STUDENTS_READ_MEDICAL)
    student = session.query(Student).get(student_id)
    ...
    # 寫 medical_access_log
    log = MedicalAccessLog(
        user_id=current_user.id,
        student_id=student_id,
        field_name="bundle",  # 一次讀全部
        reason=reason,
        ip_address=request.client.host,
    )
    session.add(log)
    session.commit()

    return {
        "allergy": student.allergy,        # 自動解密
        "medication": student.medication,
        "special_needs": student.special_needs,
    }
```

**新增 Permission**: `STUDENTS_READ_MEDICAL`（與既有 STUDENTS_READ 分開——讀基本資料不需此權限，讀醫療才需）
- 加入 `ROLE_TEMPLATES`: principal / teacher / homeroom_teacher 預設有
- accountant / admin（非教師）不預設有，需明確 grant

### 3.6 Backfill script (新檔 `scripts/encrypt_medical_fields.py`)

```python
"""一次性加密既有明文 medical 欄位。

執行：python scripts/encrypt_medical_fields.py --dry-run / --execute
"""
def main():
    # 逐筆掃 Student.allergy / medication / special_needs
    # 若 Fernet decrypt 失敗（=plaintext），則 encrypt + UPDATE
    # 用 batch（1000 列）+ commit
    # 完成後印 stats
```

### 3.7 不變的契約

- API 簽章不變（caller 看到的仍是字串）
- DB Text column 結構不變，僅內容變 base64
- 既有 SELECT 仍可用，只是 raw query 看到的是密文

---

## 4. 測試策略

### 4.1 Unit tests `tests/test_medical_encryption.py`
1. encrypt → decrypt round-trip 字串還原
2. None / empty 不加密
3. 不同 plaintext encrypt 結果不同（Fernet 含 IV）
4. InvalidToken 觸發 → 回原 ciphertext（legacy）
5. Key 未設 → RuntimeError

### 4.2 ORM tests `tests/test_models_medical.py`
1. Student.allergy = "花粉" → session.commit → 重新 query → 自動解密回 "花粉"
2. raw SQL SELECT → 看到密文
3. None 寫入 → None 讀出
4. contact_book temperature_c 字串化 + 加解密

### 4.3 API tests
1. `GET /students/{id}/medical` without reason → 422
2. with reason ≥10 字 → 200 + `medical_access_log` 寫入
3. with reason < 10 字 → 422
4. 無 STUDENTS_READ_MEDICAL 權限 → 403
5. 既有 `GET /students/{id}` 仍可看 name/birthday 等非醫療欄位（不影響）

### 4.4 Migration tests
- migration up + down 不破壞既有資料
- ORM 跑加密前後 SELECT 結果一致（透過 TypeDecorator）

### 4.5 Backfill test
- 跑 backfill script 對 100 列 plaintext student → 全部變密文 → SELECT via ORM 自動解回
- Idempotency: 重跑 backfill 不重複加密（detect Fernet 已 encrypted）

---

## 5. Rollout

1. **PR1 (BE schema + helper)**: migration + utils/medical_encryption + EncryptedText TypeDecorator + tests
2. **PR2 (BE model migration + ORM transparent)**: 改 model column type + 確認既有 query 正常（rollback-safe，因 column 仍是 Text）
3. **PR3 (BE access log + reason endpoint)**: medical_access_log + STUDENTS_READ_MEDICAL permission + 改 endpoint
4. **PR4 (FE)**: 教師端 ChildMedicalView 加 reason input 流程
5. **Cutover**: deploy → backfill script ops 安排 maintenance window 跑（線上業務影響：執行期間 query 略慢，但 ORM 端透明，無 downtime）

### 5.1 Key Management
- env `MEDICAL_FIELD_ENCRYPTION_KEY` 用 `Fernet.generate_key()` 產生
- prod key 存 zeabur env，dev key 各自 generate 進 .env（不 commit）
- key rotation：v1 不做，follow-up 用 MultiFernet 支援雙 key window

---

## 6. Risk & Trade-offs

### 6.1 已接受的 Risk

| Risk | 接受理由 | Follow-up |
|------|---------|-----------|
| 既有 plaintext 等 ops backfill | DBA window 控制風險 | backfill script 含 dry-run |
| temperature_c 失去 range query | 業務未見此需求（先 grep 確認） | 改 deterministic encryption + index |
| height/weight 不加密 v1 | 需 range query 不可破 | follow-up deterministic encryption |
| 單 key 無 rotation | v1 scope | MultiFernet follow-up |
| Backfill 期間 row 可能同時被 update → race | backfill 用 SELECT FOR UPDATE 或限制 maintenance window | ops 確認 |

### 6.2 對其他 sprint 的依賴
- 依賴 P0b（audit redact）：避免 access log 寫入時 reason 含 PII 被遮（reason 屬非 PII 業務說明，不在 denylist）
- 依賴 P0c（reason / audit-trail pattern）：取用 reason 與 consent log 都是「操作前先記錄」pattern；P0c 先做完 medical_access_log 設計更熟

### 6.3 Performance
- Fernet decrypt 約 100μs/row。一頁 50 學生讀 medical = 50 × 100μs × 3 column = 15ms 額外延遲，可接受
- contact_book temperature 每日 N 學生顯示 → cache friendly

---

## 7. 驗收條件

1. UPDATE student.allergy = "花粉" → DB raw SELECT 看到 `gAAAAAB...` base64 密文
2. ORM session.query(Student).first().allergy → "花粉" 自動解密
3. `GET /students/1/medical` without reason → 422
4. with `reason="2026-05-28 老師回報過敏反應評估" ` → 200 + medical_access_log 寫入
5. backfill script dry-run → 列出待加密筆數但不寫
6. backfill script execute → 全部 plaintext 變密文 + ORM 仍可讀
7. 既有 pytest 5103+ 全綠 + 新增 medical encryption / model / endpoint / backfill test 全綠
