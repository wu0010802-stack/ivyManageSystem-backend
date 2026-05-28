# EXIF Strip 進入點清洗（P0a 兒童照片位置個資）

**日期**: 2026-05-28
**範圍**: ivy-backend
**Sprint**: P0a（4 個 P0 法規/個資 sprint 中的第一個 quick win）
**預估**: 1.5 工作天

---

## 1. 背景與動機

兒童照片原檔含 GPS EXIF metadata，可從家庭住址、上學動線反查兒童位置，屬個資法 §6 特種個資（兒童位置高敏）、COPPA §312.4(b)、GDPR Recital 51 範疇。

**現況證據：**
- `utils/portfolio_storage.py:184` `put_attachment` 直接 `write_bytes(content)`，完整保留 iPhone 拍照 GPS + 相機 ID + 拍攝時間。
- `_apply_exif_orientation`（line 264-269）只處理 display/thumb variants 的方向，原檔 EXIF 未清。
- 家長端 `photos.py:_parent_url_for_key` 給原檔 URL，下載後可用 `exiftool` 提取 GPS。

**風險**：兒童家庭住址、上學動線從 EXIF GPS 反查 → 被惡意人士跟蹤；單一家長截圖 EXIF 給媒體即構成資安事件需 72hr 通報。

---

## 2. 目標與非目標

### 目標
1. 所有家長端可下載的兒童照片原檔，**不得含 GPS、相機序號、拍攝裝置識別 metadata**。
2. 保留照片正向顯示（Orientation 套到像素後丟棄 tag）。
3. 平台基線：任何走 `read_upload_with_size_check` 的 image 上傳自動清洗，未來新增 caller 自動受惠。

### 非目標（明確不做）
1. **既有原檔批次 backfill**：disk 上既有 EXIF 不批次清除。Risk-accepted：v1 ship 起新檔乾淨即大幅降低攻擊面；backfill 需 storage backend ops 配套（local + Supabase 兩 backend），列為 follow-up。
2. **HEIC/HEIF/GIF 原檔 strip**：HEIC 走 portfolio variants transcode 已等價於 strip；GIF 非主要威脅（少見 GPS tag）；HEIC client 端 view 通常需要 transcode，原檔直接 leak 風險低。列為 follow-up。
3. **影片 metadata strip**：MP4/MOV 也含 GPS 但本 sprint 不做（需 ffmpeg 設施，scope 大）。列為 follow-up。
4. **audit log / model 欄位 PII 遮罩**：那是 P0b 範圍。

---

## 3. 設計

### 3.1 新增 `utils/image_sanitize.py`

**純函式 helper**，無 I/O、無 side effect：

```python
def strip_image_metadata(content: bytes, ext: str) -> bytes:
    """清除影像 metadata，回傳乾淨 bytes。

    支援格式：.jpg / .jpeg / .png / .webp
    其他 ext：直接回傳原 content（無 transformation）

    處理：
      - 用 Pillow 開圖 + ImageOps.exif_transpose 把 Orientation 套到像素
      - 重 encode 不寫 EXIF/XMP/ICC profile
      - JPEG: quality="keep"（保留原 quantization table）
      - PNG: optimize=False
      - WebP: quality=85

    錯誤：
      - PIL 解析失敗 → HTTPException(400, "影像格式不支援或損毀")
      - DecompressionBombError → HTTPException(400, "影像尺寸超過上限")
      - 重 encode 失敗 → HTTPException(500), logger.error + 不靜默回原檔
    """
```

**關鍵實作要點**：
- JPEG `quality="keep"` 為 Pillow 特殊 mode，保留原 quantization table 不損畫質。
- WebP 無 `quality="keep"` 選項，固定 85。
- PNG 重 encode 不指定 quality，`optimize=False` 避免改變 IDAT chunk 結構。
- **完全捨棄 ICC profile**：colour profile 可能含 device identifier；對家長端展示無顯著影響。
- **完全捨棄 XMP/IPTC**：Adobe metadata 可能含建立者 / GPS 副本。

### 3.2 整合 `utils/file_upload.py:read_upload_with_size_check`

**將 validate + strip 都移進 helper 內部**，順序保證為：chunked read → size check → magic_bytes validate → strip_image_metadata（若 image ext）。

```python
async def read_upload_with_size_check(
    file: UploadFile,
    *,
    extension: str | None = None,
) -> bytes:
    # 1. 既有 chunked read with size limit（不變）
    limit = max_upload_size_for(extension) if extension else MAX_UPLOAD_SIZE
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            ...  # 原邏輯
        chunks.append(chunk)
    content = b"".join(chunks)

    # 2. 新增：magic_bytes 驗證（從外部 caller 移進來）
    if extension:
        validate_file_signature(content, extension)

    # 3. 新增：image 進入點 EXIF 清洗（必在 validate 之後）
    if extension and extension.lower() in IMAGE_EXTENSIONS_TO_SANITIZE:
        from utils.image_sanitize import strip_image_metadata
        content = strip_image_metadata(content, extension)

    return content
```

`IMAGE_EXTENSIONS_TO_SANITIZE = {".jpg", ".jpeg", ".png", ".webp"}` 定義於 `image_sanitize.py`。

**對既有 caller 的影響**：
- 既有 13+ 處 caller 在 `read_upload_with_size_check` 後仍 call `validate_file_signature(content, ext)` 是**冗餘但無害**（idempotent — 對已驗證內容再驗一次仍 pass）。
- v1 PR 維持冗餘不動既有 caller，避免改動 scope 擴大。**cleanup 列為 follow-up**（一輪掃尾 PR 移除冗餘 validate 呼叫）。
- 若 caller **不傳 extension**（罕見 legacy），validate 與 strip 都不會觸發，行為與今日完全一致（向後相容）。

### 3.3 Bypass path 修復（4 處）

以下 path 目前直接 `await file.read()` 繞過 `read_upload_with_size_check`，**順帶**改為走 helper：

| Path | 行 | 是否 image | 動作 |
|------|----|---------|------|
| `api/parent_portal/events.py` | 254 | **是**（家長活動照片） | 改走 helper，自動 strip |
| `api/portal/leaves.py` | 498 | **是**（員工請假可能含證明照片） | 改走 helper，自動 strip |
| `api/appraisal/__init__.py` | 1417 | 否（考核附件，PDF/XLSX 為主） | 改走 helper 補 size check |
| `api/year_end/__init__.py` | 424 | 否（年終附件） | 改走 helper 補 size check |

修復方式（validate 與 strip 都已在 helper 內部處理，bypass-fix 只需呼叫 helper）：
```python
# Before:
content = await file.read()

# After:
ext = os.path.splitext(file.filename or "")[1].lower()
content = await read_upload_with_size_check(file, extension=ext)
# 不需另外呼叫 validate_file_signature — helper 已內部處理
```

**檔案層級擋格式**：4 個 bypass path 若原本沒擋副檔名（如只接 PDF），需先在 helper 呼叫前加 `if ext not in ALLOWED_EXT: raise HTTPException(400)`，避免上傳 .jpg 到 appraisal 後被 silent strip。實際對 appraisal/year_end 影響為零（callers 預期格式為 PDF/XLSX 不在 image set，不會觸發 strip）。

---

## 4. 測試策略

### 4.1 Fixtures（`tests/fixtures/exif/`）

**新增三個樣本**（要 commit 進 repo）：
- `with_gps.jpg`：含 GPS EXIF（手工用 piexif 或 exiftool 寫入測試 GPS coordinates 25.0, 121.5）
- `with_orientation_6.jpg`：橫拍 + Orientation=6 標記（需要旋轉 90° CW 顯示）
- `clean.png`：對照組無 metadata

Fixture 生成腳本放 `tests/fixtures/exif/_generate.py`（committed for reproducibility）。

### 4.2 Unit tests `tests/test_image_sanitize.py`（新檔）

1. **GPS 清除**: `strip_image_metadata(with_gps.jpg, ".jpg")` 後 `Image.open(BytesIO(out)).getexif()` 不含 `GPSInfo` tag (34853)。
2. **Orientation 套到像素**: `strip_image_metadata(with_orientation_6.jpg, ".jpg")` 後：
   - `getexif().get(274)` （Orientation tag）為 1（normal）或不存在
   - 像素 size 反映已旋轉（width/height 對調）
3. **格式外不動**: `strip_image_metadata(b"%PDF-1.4", ".pdf")` 回傳 `b"%PDF-1.4"`
4. **不支援的 image ext**: `strip_image_metadata(content, ".heic")` 回傳原 content（v1 不處理 HEIC）
5. **損毀檔案**: `strip_image_metadata(b"not an image", ".jpg")` raise `HTTPException(400, "影像格式不支援或損毀")`
6. **PNG**: `strip_image_metadata(png_with_textual_metadata, ".png")` 後 `Image.open().info` 不含 tEXt/iTXt chunks
7. **WebP**: `strip_image_metadata(webp_with_exif, ".webp")` 後 metadata 已清
8. **DecompressionBomb**: 用 mock 觸發 Pillow `DecompressionBombError` → raise HTTPException(400)
9. **image dimension 不變**: with_gps.jpg 是 100x100，strip 後仍 100x100（Orientation=1 不旋轉時）

### 4.3 Integration tests `tests/test_file_upload.py`（擴充既有檔）

1. **read_upload 自動清洗**: 包 with_gps.jpg 成 UploadFile，呼叫 `read_upload_with_size_check(file, extension=".jpg")`，回傳 bytes 用 `Image.open().getexif()` 確認 GPS gone。
2. **非 image ext 不清洗**: 包 PDF binary，呼叫 helper 後 bytes 與原本一致（byte-by-byte）。
3. **既有 size limit 行為不變**: 大檔仍 raise 400 with "檔案超過 10MB 限制"。
4. **既有 magic_bytes 行為不變**: 偽報的 ext 仍 raise 400 with "檔案內容與副檔名不符"。

### 4.4 Bypass path regression tests

4 個 bypass path 既有的 test 不改（行為向後相容：non-image 上傳仍正常）。**新增** 1 個 integration test：呼叫 `api/parent_portal/events.py` 的 upload endpoint 上傳 with_gps.jpg → 從 DB 查到 file_path → 讀 disk 上實體檔 → 確認 GPS gone。

### 4.5 Verification 手動驗證（PR merge 前）

1. local 啟 backend，用 portfolio upload endpoint 上 with_gps.jpg
2. `exiftool data/uploads/portfolio/<key>` 確認無 GPS tag
3. 從家長端下載原檔 URL，本機 `exiftool downloaded.jpg` 再次確認
4. 視覺確認 Orientation=6 樣本下載後正向顯示

---

## 5. Rollout

1. **PR**: 含 `utils/image_sanitize.py` 新檔、`utils/file_upload.py` hook、4 bypass fix、tests、fixtures。
2. **CI**: 全 pytest 通過 + 既有 5103 test no regression。
3. **No schema migration**：純 code change，不動 DB。
4. **No frontend change**：family-facing URL/contract 不變，只是檔案內容已清洗。
5. **Merge & push**：合到 main 即生效，下一次部署後新上傳的 image 自動清洗。

---

## 6. Risk & Trade-offs

### 6.1 已接受的 Risk

| Risk | 接受理由 | Follow-up |
|------|---------|-----------|
| 既有原檔不 backfill，存量檔案 EXIF 還在 | 攻擊面從 ship 起每日下降；backfill 是 storage ops 配套較重 | follow-up `backfill-image-exif-strip.py` script，先掃 local + Supabase 兩 backend 計總量再評估執行視窗 |
| HEIC 原檔不清 | portfolio variants transcode 為 JPG 等同 strip；客戶端 view HEIC 通常已 transcode | follow-up 加 HEIC 支援（需 pillow-heif 套件）|
| 影片 metadata 不清 | 影片 GPS 較罕見，ffmpeg 設施重 | follow-up，需 ops 評估 ffmpeg 部署 |
| ICC profile 一律丟棄 | colour management 對家長端可忽略，profile 可能含 device id | 無 |

### 6.2 可能爭議

- **JPEG `quality="keep"` 仍涉及重 encode**：理論畫質完全保留，但 Pillow 內部處理可能改變 Huffman table。實測檔案大小通常 ±2% 內可接受。
- **WebP `quality=85`**：對 lossy WebP 是業界 default；對 lossless WebP 重 encode 為 lossy 會降畫質。**第一版只支援 lossy WebP**，lossless 列為 follow-up（或全擋掉 `WebP.info.get("lossless")` 用 lossless=True 保畫質但檔案大 2-3 倍）。

### 6.3 不破壞的契約

- API request/response schema：不變
- Storage key 生成邏輯：不變
- 家長下載 URL 格式：不變
- file size limit：不變
- magic_bytes 驗證：不變

---

## 7. 與 P0b-d 的關係

| Sprint | 範圍 | 與 P0a 關係 |
|--------|------|-----------|
| P0b: Audit log PII redaction + retention GC | utils/audit.py 套 Sentry denylist + security_gc_scheduler 加 audit_log 7y/3y/6m | 獨立 |
| P0c: Consent infrastructure + DSR completion | parent_consent_log 表 + policy versioning + LIFF modal + MeView 加 delete/correct/objection | 獨立 |
| P0d: 醫療欄位 application-level 加密 + medical_access_log | pgcrypto 加密 allergy/medication/special_needs + 取用 reason 欄 | 依賴 P0c 的 reason 欄位基礎 |

P0a 不阻擋任何 P0b-d，4 sprint 可序貫或並行（如有 capacity）。

---

## 8. 驗收條件

PR merge 進 main 並 ship 後，下列全部成立：
1. 上傳 with_gps.jpg 至 portfolio → 下載原檔 → `exiftool` 確認無 GPSInfo / Make / Model / Software tags
2. 上傳 with_gps.jpg 至 parent_portal/events → 同上
3. 上傳 with_gps.jpg 至 portal/leaves → 同上
4. 上傳 Orientation=6 樣本 → 下載後正向顯示
5. 上傳 corrupted JPG → 回 400 "影像格式不支援或損毀"
6. 上傳 PDF → 仍正常（不被誤套 image strip）
7. 既有 pytest 5103+ 全綠 + 新增 image_sanitize / file_upload integration / bypass regression test 全綠
