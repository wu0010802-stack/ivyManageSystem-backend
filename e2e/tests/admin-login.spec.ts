import { test, expect } from '@playwright/test'

test.describe('@smoke admin login UI', () => {
  test('用 admin 帳密登入後 redirect 到首頁（或改密頁）', async ({ page }) => {
    const username = process.env.E2E_ADMIN_USERNAME!
    const password = process.env.E2E_ADMIN_PASSWORD!

    await page.goto('/login')
    await expect(page.getByRole('heading', { name: /管理員登入/ })).toBeVisible()

    await page.getByPlaceholder(/帳號|工號|使用者/).first().fill(username)
    await page.getByPlaceholder(/密碼/).first().fill(password)
    await page.getByRole('button', { name: /^登\s*入$|登入系統|登 入/ }).click()

    await page.waitForURL(/\/(change-password)?$/, { timeout: 10_000 })
    expect(page.url()).toMatch(/\/(change-password)?$/)
  })

  test('錯誤密碼顯示錯誤訊息', async ({ page }) => {
    const username = process.env.E2E_ADMIN_USERNAME!

    await page.goto('/login')
    await page.getByPlaceholder(/帳號|工號|使用者/).first().fill(username)
    await page.getByPlaceholder(/密碼/).first().fill('definitely-wrong-pwd-' + Date.now())
    await page.getByRole('button', { name: /^登\s*入$|登入系統|登 入/ }).click()

    await expect(page.getByText(/失敗|錯誤|不正確|無效/).first()).toBeVisible({ timeout: 8_000 })
    expect(page.url()).toMatch(/\/login/)
  })
})
