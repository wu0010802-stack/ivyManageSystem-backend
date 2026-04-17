# 幼稚園人事差勤與薪資管理系統 — Backend

本系統專為幼稚園與補教機構設計，提供完整的人事管理、排班考勤、請假審核、以及自動化薪資結算功能，並支援員工個人入口網站 (Portal)。

前端為獨立 repo：[`ivyManageSystem-frontend`](https://github.com/wu0010802-stack/ivyManageSystem-frontend)。

## 🚀 核心功能特色

### 1. 人事管理 (Employee Management)
- 詳細管理員工基本資料、身分（正職、才藝老師等）、職稱、到職日與投保級距。
- 支援分校與部門設定，彈性的人員查詢與匯出。

### 2. 排班與考勤 (Shift & Attendance)
- **排班管理**：支援預設班制、每週排班以及跨日/單日特殊調班設定。
- **國定假日設定**：系統內建行事曆，可自訂國定假日。
- **考勤紀錄**：支援匯入打卡資料，自動比對排班時間，產出遲到、早退、曠職及加班異常時數。

### 3. 請假管理與配額計算 (Leave Management & Quotas)
- 支援台灣勞基法規範之各類假別 (事假、病假、特休、婚假、喪假、產假等) 與相對應的扣薪規則。
- **自動化配額**：依據到職日自動計算特休天數，並自動帶入病假、事假、生理假等法定額度。
- **時數結算**：請假可精確至自訂時間區段，並自動扣除午休時間以計算實際請假工時。
- 支援管理員審核與退回（附駁回原因）。

### 4. 自動薪資引擎 (Salary Engine)
- 整合底薪、職務加給、排班時薪、各類獎金與請假扣薪。
- **勞健保扣繳**：內建最新的台灣勞健保級距對照，支援自動扣繳保費（可自訂版本升級）。
- **節日與註冊獎金**：可依職位或負責班級註冊人數，動態結算各種節慶與超額獎金。
- 支援薪資單與銀行轉帳清單產生與匯出。

### 5. 員工專屬入口 (Employee Portal)
- 員工個人首頁與內部公告欄。
- 線上請假與加班申請申請。
- 個人出缺勤紀錄與薪資單明細即時查詢。

## 🛠 技術堆疊

- **Framework**: FastAPI (Python 3)
- **Database**: PostgreSQL (透過 SQLAlchemy ORM, Alembic migration)
- **Authentication**: JWT (python-jose) + PBKDF2 密碼雜湊 + RBAC
- **Data Processing**: Pandas (Excel 匯入/處理)

## 📦 安裝與啟動

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8088
```

啟動後：
- API：`http://localhost:8088`
- Swagger UI：`http://localhost:8088/docs`

## 環境變數

複製 `.env.example` 為 `.env`。

招生生活圈若要改走 Google API，至少需要：

```env
GOOGLE_MAPS_API_KEY=your-backend-google-maps-api-key
```

這把 key 建議只開：
- `Geocoding API`
- `Routes API`
- `Places API`

若要在招生統計頁直接同步義華校官網後台資料，再補上：

```env
IVYKIDS_USERNAME=your-ivykids-backend-account
IVYKIDS_PASSWORD=your-ivykids-backend-password
```

如需改站台路徑，也可另外設定：

```env
IVYKIDS_LOGIN_URL=https://www.ivykids.tw/manage/
IVYKIDS_DATA_URL=https://www.ivykids.tw/manage/make_an_appointment/
```

若要讓 backend 每 10 分鐘自動同步一次，再補上：

```env
IVYKIDS_SYNC_ENABLED=true
IVYKIDS_SYNC_INTERVAL_MINUTES=10
```

### 部署
直接 `git push origin main`，由 GitHub Actions 跑 CI（`.github/workflows/ci.yml`）。

## 📁 目錄結構
```
backend/
├── main.py              # App 建立、CORS、Router 註冊
├── startup/             # 啟動邏輯（seed、migration、bootstrap）
├── api/                 # API Routers（40+ 個）
├── services/            # 商業邏輯（SalaryEngine、InsuranceService、LineService…）
├── models/              # SQLAlchemy models + 連線管理
├── utils/               # 工具模組（auth、audit、rate_limit 等）
├── alembic/             # DB migration
└── tests/               # pytest 測試
```

## 📝 授權與維護
Internal usage only.
