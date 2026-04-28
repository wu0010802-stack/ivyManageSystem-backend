# 課後才藝收費漏洞專項檢查

檢查日期：2026-04-28  
範圍：`api/activity/*`、`models/activity.py`、活動金流相關測試。

## 摘要

課後才藝收費主流程已有多項防護：POS 結帳會擋超收、退費不得超過已繳、退費需原因、大額退費需 `ACTIVITY_PAYMENT_APPROVE`、付款刪除改為 void、已日結日期不可再改金流、公開端更新若會產生退費會被拒絕。

本次仍找到 2 個應修的收費漏洞與 1 個低風險帳務報表問題：

1. **高風險**：後台單筆新增付款可繞過 POS 超收守衛，任意把報名寫成 overpaid。
2. **中風險**：複製上學期課程可旁路「高價課程需簽核」規則。
3. **低風險**：繳費報表的「最後繳費日」會把已 void 的付款也算進去。

## F-01：後台單筆新增付款可繞過超收守衛

- Severity：High
- Location：
  - `api/activity/pos.py:556-571`
  - `api/activity/registrations.py:2239-2448`
- Evidence：
  POS 結帳路徑明確禁止超收：
  ```python
  if (reg.paid_amount or 0) + item.amount > total_amount_pre:
      raise HTTPException(... "將導致超收" ...)
  ```
  但後台單筆新增付款只檢查「是否為空報名」，沒有檢查 `paid_amount + amount <= total_amount`：
  ```python
  if body.type == "payment":
      current_total = _calc_total_amount(session, registration_id)
      if current_total <= 0:
          raise HTTPException(...)
  ...
  if body.type == "payment":
      reg.paid_amount = (reg.paid_amount or 0) + body.amount
  ```
- Attack / abuse scenario：具備 `ACTIVITY_WRITE` 的一線帳號，可對應繳 NT$1,000 的報名呼叫：
  `POST /api/activity/registrations/{id}/payments`，送 `amount=999999`，使該報名變成 `overpaid`，並讓日結、報表、收入統計出現巨額假收款。這條路徑不需要 `ACTIVITY_PAYMENT_APPROVE`，也沒有 POS 的單次總額上限。
- Impact：內部舞弊或誤操作可直接污染金流資料；後續需要 void 或退費沖帳才能修正，若已日結會更難處理。
- Fix：讓單筆新增付款與 POS 規則一致：
  ```python
  if body.type == "payment":
      current_total = _calc_total_amount(session, registration_id)
      remaining = current_total - (reg.paid_amount or 0)
      if body.amount > remaining:
          raise HTTPException(status_code=400, detail="本次收款將導致超收")
  ```
  若業務真的需要預收/溢收，建議新增明確的「預收款」模型或要求 `ACTIVITY_PAYMENT_APPROVE` + 原因，而不是讓一般付款路徑隱性支援 overpaid。
- Suggested regression test：新增測試確認 `POST /api/activity/registrations/{id}/payments` 對 `payment` 超過 remaining 時回 400，且不寫入 `ActivityPaymentRecord`。

## F-02：複製課程可旁路高價課程簽核

- Severity：Medium
- Location：
  - `api/activity/courses.py:214-217`
  - `api/activity/courses.py:258-274`
  - `api/activity/_shared.py:91-108`
- Evidence：
  建立/更新課程有高價簽核：
  ```python
  require_approve_for_high_price(body.price, current_user, ...)
  ```
  但 `copy_courses_from_previous` 只需要 `ACTIVITY_WRITE`，直接複製 `src.price`：
  ```python
  @router.post("/courses/copy-from-previous", status_code=201)
  async def copy_courses_from_previous(
      body: CopyCoursesRequest,
      current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
  ):
      ...
      new_course = ActivityCourse(
          name=src.name,
          price=src.price,
          ...
      )
  ```
- Attack / abuse scenario：若來源學期已有高價課程（歷史資料、匯入資料、或主管先前建立），只有 `ACTIVITY_WRITE` 的使用者可把它複製到新學期，繞過「超過 NT$30,000 需才藝課收款簽核」的控制。
- Impact：高額應收可被延續到新收費期，搭配批次補齊或單筆付款會放大帳務污染風險。
- Fix：複製前掃描來源課程價格；任一課程超過 `ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD` 時，要求 `ACTIVITY_PAYMENT_APPROVE`。也可在回應中列出 skipped high-price courses，要求主管另行複製。
- Suggested regression test：建立一筆來源高價課程，使用只有 `ACTIVITY_WRITE` 的帳號呼叫 `copy-from-previous` 應回 403；具備 `ACTIVITY_PAYMENT_APPROVE` 則允許。

## F-03：付款報表最後繳費日包含已作廢紀錄

- Severity：Low
- Location：`api/activity/registrations.py:348-368`
- Evidence：
  報表撈付款明細時沒有排除 `voided_at`，並用所有 payment/refund 的 `payment_date` 更新 `last_payment_date_map`：
  ```python
  payment_records = (
      session.query(ActivityPaymentRecord)
      .filter(ActivityPaymentRecord.registration_id.in_(reg_ids))
      ...
      .all()
  )
  for pr in payment_records:
      ...
      if date_str > existing:
          last_payment_date_map[pr.registration_id] = date_str
  ```
- Impact：已作廢的付款仍可能顯示成總覽頁的最後繳費日，造成對帳或催繳判斷誤導。金額總覽仍使用 `reg.paid_amount`，所以不是直接金額竄改。
- Fix：`last_payment_date_map` 應只納入 `voided_at IS NULL` 且建議只納入 `type == "payment"`；voided 紀錄留在明細工作表獨立標示即可。

## 已確認的既有防護

- POS checkout：禁止超收、退費超過已繳、重複 registration id、已日結日期寫入。
- Refund：退費原因必填，累積退費超過閾值需 `ACTIVITY_PAYMENT_APPROVE`。
- Delete payment：需 `ACTIVITY_PAYMENT_APPROVE`，採 void，不實體刪除。
- Public update：若更新後會導致超繳，直接 409，避免前台自動退費。
- Course/Supply create/update：單價上限 `MAX_PAYMENT_AMOUNT`，高價需簽核。

## 建議修復順序

1. F-01 先修，因為它是最直接的收費寫入旁路。
2. F-02 接著修，補齊高價課程控制的一致性。
3. F-03 可排在報表口徑整理時一併修。
