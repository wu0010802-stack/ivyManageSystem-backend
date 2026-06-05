import { test, expect, type ConsoleMessage } from '@playwright/test'

const PAGES: Array<{ path: string; label: string }> = [
  { path: '/', label: '首頁' },
  { path: '/attendance', label: '考勤' },
  { path: '/leaves', label: '請假' },
  { path: '/salary', label: '薪資' },
]

const IGNORE_PATTERNS = [
  /\[Vue Router warn\]/i,
  /\[Vue warn\].*Failed to resolve component/i,
  /\[Sentry\]/i,
  /favicon\.ico/i,
  /DevTools/i,
  /WebSocket connection.*ws:\/\/localhost:5173/i,
  /sourceMappingURL/i,
]

for (const { path, label } of PAGES) {
  test(`@smoke admin 頁載入：${label} (${path})`, async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg: ConsoleMessage) => {
      if (msg.type() !== 'error') return
      const text = msg.text()
      if (IGNORE_PATTERNS.some((re) => re.test(text))) return
      errors.push(text)
    })
    page.on('pageerror', (err) => {
      errors.push(`pageerror: ${err.message}`)
    })

    const res = await page.goto(path)
    expect(res?.status() ?? 0, `${path} HTTP status`).toBeLessThan(400)

    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {})

    expect(errors, `${path} console.error / pageerror:\n${errors.join('\n')}`).toEqual([])
  })
}
