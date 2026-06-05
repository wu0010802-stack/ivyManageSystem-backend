import { test, expect } from '@playwright/test'

test.describe('@smoke health', () => {
  test('未登入打 /api/auth/me 應 401（驗後端在跑 + auth 守衛存在）', async ({ request }) => {
    const res = await request.get('/api/auth/me')
    expect(res.status()).toBe(401)
  })

  test('前端 root 載入 200（驗 Vite dev server 在跑）', async ({ page }) => {
    const res = await page.goto('/')
    expect(res?.status()).toBeLessThan(400)
  })
})
