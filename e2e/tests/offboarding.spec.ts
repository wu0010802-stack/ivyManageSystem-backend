import { test, expect } from '@playwright/test'

// admin storageState 由 globalSetup 預建（同其他 auth-admin spec 共用）。
// 員工離職 critical-path: admin login → 開 OffboardingModal → preview → confirm → 驗 step result。
//
// 注意：此 spec 會真實寫 DB（Employee.resign_date / is_active / offboarding_record）
// 跑前確認 .env E2E_TEST_EMPLOYEE_ID 是測試專用且狀態可被破壞性測試。
// admin 不可寫自己（self-guard），E2E_TEST_EMPLOYEE_ID ≠ admin.employee_id。
//
// CI / 例行跑：set TEST_OFFBOARDING_DESTRUCTIVE=1 解鎖 destructive test；
// 否則 skip 避免污染 DB。

const TARGET_EMP_ID = parseInt(process.env.E2E_TEST_EMPLOYEE_ID || '0')
const RUN_DESTRUCTIVE = process.env.TEST_OFFBOARDING_DESTRUCTIVE === '1'

test.describe('員工離職 critical path', () => {
  // fixme（2026-06-05 e2e CI 接上時發現）：此 spec 依賴瀏覽器登入態，但現行 harness
  // 的 globalSetup 只在 storageState 放 httpOnly cookie（API 認證足夠），未放前端判定
  // 登入用的 localStorage['userInfo'] → 瀏覽器頁面被 router guard 導去 /login。
  // 注入 userInfo 可讓瀏覽器登入，但與後端 staff-refresh-rotation 衝突（refresh 輪替
  // token → 共用 storageState cookie 失效 → 其他 API spec 401）。真正修法需 harness
  // 重設計（每 spec 獨立認證 / 測試環境關輪替），列 follow-up。
  // 附帶：本 spec 此次「順便抓到」一個真 route bug——/admin/offboarding 漏列
  // ROUTE_PERMISSION_RULES 致 default-deny 鎖死全員（已修於前端分支
  // fix/offboarding-route-perm-2026-06-05-fe，待併 main）。
  test.fixme('@smoke 離職管理清單頁渲染', async ({ page }) => {
    await page.goto('/admin/offboarding')
    await expect(page.getByRole('heading', { name: /離職管理/ })).toBeVisible({
      timeout: 5_000,
    })
    // 至少有 table 元素（不驗 row 內容，避免 DB 狀態依賴）
    await expect(page.locator('table')).toBeVisible({ timeout: 5_000 })
  })

  test('admin 一鍵離職 → preview → confirm → 5 step result', async ({ page }) => {
    test.skip(!TARGET_EMP_ID, 'E2E_TEST_EMPLOYEE_ID not set')
    test.skip(!RUN_DESTRUCTIVE, 'destructive test — set TEST_OFFBOARDING_DESTRUCTIVE=1 to run')

    await page.goto('/employees')

    // 找到 target 員工 row 內「辦理離職」按鈕
    const targetRow = page
      .locator('tr')
      .filter({ hasText: new RegExp(`emp-${TARGET_EMP_ID}|${TARGET_EMP_ID}`) })
      .first()
    await targetRow.getByRole('button', { name: /辦理離職/ }).click()

    // OffboardingModal 開啟（input stage）
    const dialog = page.getByRole('dialog', { name: /辦理離職/ })
    await expect(dialog).toBeVisible()

    // Stage 1: input
    await dialog.locator('input[type="date"]').fill('2026-06-15')
    await dialog
      .getByPlaceholder(/離職證明 PDF|內部留存/)
      .fill('e2e smoke test')
    await dialog.getByRole('button', { name: '預覽' }).click()

    // Stage 2: preview — 顯示帳號撤銷狀態
    await expect(dialog.getByText(/將撤銷|通知期保留/)).toBeVisible({
      timeout: 10_000,
    })
    await dialog.getByRole('button', { name: '確認辦理' }).click()

    // Stage 3: result — 5 step label 至少 4 個顯示（generate_certificate 可能因 disk/font fail）
    await expect(dialog.getByText('標記進行中考核')).toBeVisible({ timeout: 15_000 })
    await expect(dialog.getByText('特休餘額快照')).toBeVisible()
    await expect(dialog.getByText('撤銷使用者帳號')).toBeVisible()
    await expect(dialog.getByText('產生離職證明 PDF')).toBeVisible()
  })
})
