/**
 * axe-core WCAG 2.1 AA baseline scan
 *
 * 以登入後 admin 身份掃 critical-path 頁面，將 violations 累積寫入
 * `.scratch/axe-baseline.json`，供後續 markdown 報告與排序修正用。
 *
 * Baseline 模式：**不 fail test**，即使有 violations。目的是先量大小、
 * 排優先順序，再決定門檻。後續可改為 expect(violations).toEqual([]) 並
 * 將已修部分加 baseline diff。
 *
 * 跑法（兩端 dev server 已起）：
 *   set -a; . ./.env; set +a
 *   npx playwright test a11y-baseline --project=auth-admin
 */

import { test, expect } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const OUT_PATH = path.resolve(__dirname, '..', '..', '.scratch', 'axe-baseline.json')

const PAGES: Array<{ path: string; label: string }> = [
  { path: '/', label: '首頁' },
  { path: '/attendance', label: '考勤' },
  { path: '/leaves', label: '請假' },
  { path: '/salary', label: '薪資' },
  { path: '/employees', label: '員工' },
]

interface PageReport {
  path: string
  label: string
  url: string
  status: 'ok' | 'load_error' | 'axe_error'
  error?: string
  violationsByImpact: Record<string, number>
  violations: Array<{
    id: string
    impact: string | null | undefined
    help: string
    helpUrl: string
    nodeCount: number
    sampleTargets: string[]
  }>
}

const aggregate: { generatedAt: string; pages: PageReport[] } = {
  generatedAt: new Date().toISOString(),
  pages: [],
}

test.describe.serial('a11y baseline (WCAG 2.1 AA)', () => {
  for (const { path: urlPath, label } of PAGES) {
    test(`axe scan: ${label} (${urlPath})`, async ({ page }, testInfo) => {
      const report: PageReport = {
        path: urlPath,
        label,
        url: '',
        status: 'ok',
        violationsByImpact: {},
        violations: [],
      }

      try {
        const res = await page.goto(urlPath, { waitUntil: 'networkidle', timeout: 20_000 })
        report.url = page.url()
        if (!res || res.status() >= 400) {
          report.status = 'load_error'
          report.error = `HTTP ${res?.status() ?? 'no response'}`
          aggregate.pages.push(report)
          return
        }
      } catch (err) {
        report.status = 'load_error'
        report.error = (err as Error).message
        aggregate.pages.push(report)
        return
      }

      // 等等動態元素穩定（避免 axe 掃到 loading skeleton）
      await page.waitForTimeout(500)

      let results
      try {
        results = await new AxeBuilder({ page })
          .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
          .analyze()
      } catch (err) {
        report.status = 'axe_error'
        report.error = (err as Error).message
        aggregate.pages.push(report)
        return
      }

      for (const v of results.violations) {
        const impact = v.impact ?? 'unknown'
        report.violationsByImpact[impact] = (report.violationsByImpact[impact] ?? 0) + 1
        report.violations.push({
          id: v.id,
          impact: v.impact,
          help: v.help,
          helpUrl: v.helpUrl,
          nodeCount: v.nodes.length,
          sampleTargets: v.nodes.slice(0, 3).map((n) => n.target.join(' ')),
        })
      }

      aggregate.pages.push(report)

      // baseline 模式：不 fail，把 violation 數字附在報告中
      await testInfo.attach(`${label}-violations.json`, {
        body: JSON.stringify(report, null, 2),
        contentType: 'application/json',
      })
      testInfo.annotations.push({
        type: 'axe-summary',
        description: `${label}: ${results.violations.length} violation rules / ${Object.entries(
          report.violationsByImpact,
        )
          .map(([k, v]) => `${k}:${v}`)
          .join(' ')}`,
      })

      // 不 expect — baseline 模式
      expect(report.status).toBe('ok')
    })
  }

  test('write aggregate to .scratch/axe-baseline.json', async () => {
    await fs.mkdir(path.dirname(OUT_PATH), { recursive: true })
    await fs.writeFile(OUT_PATH, JSON.stringify(aggregate, null, 2), 'utf8')
    expect(aggregate.pages.length).toBe(PAGES.length)
  })
})
