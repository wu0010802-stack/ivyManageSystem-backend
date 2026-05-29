# 才藝點名：任何老師可點整堂跨班名冊（Portal）

- **日期**：2026-05-29
- **範圍**：跨前後端（ivy-backend `api/portal/activity.py` + ivy-frontend `src/views/portal/`）
- **狀態**：設計已與 user 確認，待寫 plan

---

## 1. 背景與問題

才藝課（`ActivityCourse`）由**外聘老師**授課，但**點名由學校內部老師**操作。實務上：

- 一堂才藝課（例如鋼琴課）的學生通常來自**多個不同班級**。
- 實際去隨班 / 點名的那位學校老師，**不固定**、也**不一定是這些學生的導師** —「任何一位學校老師都可能幫忙點」。

教師 portal 目前**已有**才藝點名功能（`api/portal/activity.py`：場次列表 / 場次詳情 / 批次點名三個端點），但它把點名範圍**鎖在「該老師自己帶的班級」**：透過 `_get_teacher_classroom_ids`（老師是該班 `head_teacher_id` / `assistant_teacher_id` / `art_teacher_id`）判定。

**結果**：隨班老師若不是某些學生的導師，就**看不到也點不到**才藝課裡其他班的學生，整堂課無法由一位老師點完。這與業務現實（任何老師都能幫忙點）不符。

## 2. 目標

讓**任何學校老師**（非家長的 portal 使用者）能在教師 portal **查看並點任何一堂才藝課的完整跨班名冊**，移除「自班」限制。

## 3. 非目標（YAGNI）

- ❌ 不在 `ActivityCourse` 新增授課老師欄位（`teacher_id` / `instructor_id`）。
- ❌ 不做「認領場次 / 指定隨班老師」流程。user 明確表示點名**不綁老師**。
- ❌ 不新增權限。授權沿用 router 層既有的 `require_non_parent_role`。
- ❌ 不新增「誰隨班」欄位；稽核沿用既有 `ActivityAttendance.recorded_by`（記錄是哪個帳號存的）。
- ❌ 不改 admin 端**行為**（`api/activity/attendance.py` 本來就跨班、沒有自班限制）。註：5.4 的共用 helper 重構會**編輯** admin attendance.py 改用 helper，但屬純抽取、行為不變。
- ❌ 不在此 spec 處理 portal 場次的建立 / 刪除 / Excel 匯出 / PDF 點名單（維持 admin 專屬）。

## 4. 關鍵設計判斷

目前 portal 點名的「自班範圍」其實**身兼兩個角色**：
1. **資料範圍**（teacher 只看到自班學生）；
2. **de-facto 授權**（三個端點除了 router 層 `require_non_parent_role` 外，沒有獨立的權限檢查，全靠自班 filter 當守門）。

因此真正要決定的只有一件事：**拿掉「自班」當授權依據後，用什麼取代它**。

**決議（與 user 確認）**：用 router 層既有的 `require_non_parent_role` 當授權邊界 —— 只要是非家長的 portal 使用者（= 學校老師 / 行政）都可以。理由：
- 新增的資料暴露很窄：僅「**某堂才藝場次的跨班報名名冊**」（attendance 情境），不是「老師能看全校學生」。
- admin 端**早就**讓任何 `ACTIVITY_WRITE` 員工看到完整跨班名冊（`batch_update_attendance` 無 scoping），這只是把「**僅限點名、僅限該場次**」的能力給 portal 老師。
- `require_non_parent_role` 已在 router 層擋掉家長 token（結構性 IDOR 隔離），「任何老師」天然被界定在員工範圍內，無家長外洩風險。
- 稽核：`recorded_by` 已記錄每筆 attendance 是哪個帳號存的，問責鏈不變。

## 5. 後端設計（`api/portal/activity.py`）

三個端點一起改，否則只改寫入沒用（老師找不到場次 / 看不到完整名冊，寫入也無從觸發）。

### 5.1 `GET /portal/activity/attendance/sessions`（場次列表）

**現況**：先用 `_get_teacher_classroom_ids` → `enrolled_course_ids`（自班有報名的課程）過濾場次；出席統計只算自班學生。

**改為**：
- 移除自班 / `enrolled_course_ids` 過濾，列出**所有才藝場次**（join 有效課程），支援既有 `course_id` / `start_date` / `end_date` 篩選。
- 出席統計 `recorded_count` / `present_count` 改為算**整堂**（對齊 admin `list_sessions` 的 GROUP BY 聚合，移除 `registration_id.in_(class_reg_ids)` 限制）。
- 回傳形狀維持目前的**陣列**（與現有前端相容）；不在此 spec 引入分頁（資料量為每學期課程數 × 場次數，數百筆輕量列可接受）。若日後量大，分頁列為 follow-up。

### 5.2 `GET /portal/activity/attendance/sessions/{session_id}`（場次詳情）

**現況**：用 `classroom_ids_filter` 只回自班學生；場次不存在或「無自班學生」都 collapse 成 `403`（F-010 防 id 列舉）。

**改為**：
- 不傳 `classroom_ids_filter`，回**完整跨班名冊**（呼叫 `_build_session_detail_response(session, sess, group_by=group_key)`，與 admin `get_session_detail` 一致）。
- 場次不存在 → `404 找不到場次`（對齊 admin）。
- **移除 F-010 的 403-collapse 與「無自班學生 → 403」分支**：任何老師都能看任何場次，列舉防護已無意義；保留 `404` 即可。
- 保留 `group_by=classroom` 支援（現在用來把跨班名冊**按班級分組**呈現）。

### 5.3 `PUT /portal/activity/attendance/sessions/{session_id}/records`（批次點名）

**現況**：驗證每個 `registration_id` 必須屬自班（`classroom_id IN classroom_ids`），否則整批 `403`。

**改為**：
- **移除自班限制**。
- 保留其餘有效性檢查（與 admin `batch_update_attendance` 一致）：報名 `is_active`、`match_status != 'rejected'`、且該 registration 確實報了本 session 對應課程（`RegistrationCourse.course_id == sess.course_id` 且 `status IN ('enrolled','promoted_pending')`）。
- **對齊 admin 行為**：無效的 registration **略過**（log warning），回傳 `{"ok": True, "updated": <套用數>, "skipped": <略過數>}`，不再整批 `403`。理由：UI 只會送它剛拿到的名冊，異常 reg 不應出現；略過比整批拒絕更穩健，且與 admin 統一。
- `recorded_by = current_user["username"]`（不變）。
- 維持 `student_id` 冗餘欄位回填邏輯（不變）。

### 5.4 小重構：抽共用 helper

改完後，portal 與 admin 的「**取本場次有效報名 + student_id map**」查詢邏輯幾乎相同。抽成 `api/activity/_shared.py` 的純查詢 helper（例如 `query_valid_session_registrations(session, session_course_id, reg_ids, *, classroom_ids=None)`），兩邊共用：

- admin caller：不帶 `classroom_ids`。
- portal caller：改後也**不帶** `classroom_ids`（即跨班）。

目的：消除重複、讓「有效報名」規則只有一份定義。此重構不改變 admin 行為（純抽取）。

### 5.5 （選配）response_model 對齊

origin/main 上 admin 端已在 Phase 3.5 補了 `schemas/activity_admin.py`（`ActivitySessionListOut` 等），portal endpoints 尚無 `response_model`（codegen 會是 `unknown`）。**本 spec 不要求**補 portal response_model；若實作時順手對齊可加，但列為**可選**，避免擴散 scope。

## 6. 前端設計（`ivy-frontend/src/views/portal/`）

相關檔案：`PortalActivityView.vue`、`components/activity/ActivitySessionList.vue`（場次列表）、`components/activity/ActivityRollcallDrawer.vue`（點名抽屜）。

- **場次列表**：現在會列出**全部**才藝場次（不再只有自班相關），需確保有**課程篩選 + 日期篩選**讓老師好定位；統計數字反映整堂（後端已改）。
- **點名抽屜**：名冊變跨班，預設帶 `group_by=classroom` 讓名冊**按班級分組**呈現，老師好找學生（後端已支援該參數）。
- **回應形狀**：批次點名回應新增 `skipped` 欄位，前端容錯處理（不因多一個欄位報錯；可選擇性提示「N 筆略過」）。
- **入口可見性**：確認 portal 才藝點名入口（`QuickLinksCard.vue` / router）對**所有非家長老師**可見，不被某權限擋住。若目前僅對「有帶班」老師顯示，需放寬。
- api 層（`src/api/activity.ts`）函式簽章不變（路徑相同），僅後端行為變更；若補了 response_model 則重跑 `npm run gen:api`。

## 7. 測試

### 後端 pytest（`tests/`）

- **跨班可見**：一位**沒有任何帶班**（`_get_teacher_classroom_ids` 回空）的老師，現在能：列出才藝場次、看到完整跨班名冊、跨班寫入點名。對照舊行為應為 403 / 空。
- **有效性仍守**：對「沒報該課」或已退課 / 已 rejected 的 registration 寫入 → 被**略過**，回應 `skipped` 計數正確，不污染統計。
- **家長仍被擋**：家長 token 打 portal endpoint 仍被 `require_non_parent_role` 擋（router 層既有保證；補一條回歸測試）。
- **統計正確**：列表 `recorded_count` / `present_count` 算整堂，與 admin 端同場次一致。
- **404 行為**：不存在的 `session_id` → 404（取代舊 403）。
- **共用 helper**：若抽 5.4 helper，admin `batch_update_attendance` 既有測試須維持全綠（行為不變）。

### 前端 vitest

- 場次列表渲染全部場次、課程 / 日期篩選可用。
- 點名抽屜渲染跨班名冊並按班分組。
- 批次點名回應含 `skipped` 時不報錯。

## 8. 安全 / 稽核小結

| 面向 | 設計 |
|------|------|
| 授權 | router 層 `require_non_parent_role`（擋家長）；不加新權限 |
| 資料暴露 | 由「自班學生」放寬為「才藝場次的跨班名冊」；窄且情境化，且 admin 端早已暴露同等資料 |
| 稽核 | `ActivityAttendance.recorded_by` 記錄存檔帳號（不變） |
| 移除 | portal detail 端點的 F-010 403-collapse（列舉防護在全開放後已無意義） |

## 9. 部署 / 風險

- **無 schema 變更、無 migration**（純行為調整）。
- **無新權限**，不需 seed / role 調整。
- 風險：放寬資料可見性是有意決策（已與 user 確認）；`recorded_by` 保留問責鏈。
- 前後端分開 commit（不同 repo），訊息描述同一功能。

## 10. Open questions（實作前可再確認，非阻擋）

1. 場次列表是否預設只顯示**本學期**（避免歷史場次過多）？目前設計：不預設過濾，靠課程 / 日期篩選；如 user 想要可加 `school_year`/`semester` 預設。
2. 點名抽屜分組是否一定 `group_by=classroom`，或保留「不分組」切換？目前設計：預設分組，保留現有切換 UI（若有）。
