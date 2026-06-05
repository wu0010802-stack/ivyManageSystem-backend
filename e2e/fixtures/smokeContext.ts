import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export type SmokeContext = {
  adminUsername: string
  adminEmployeeId: number | null
  targetEmployeeId: number
  targetEmployeeName: string
  apiURL: string
  baseURL: string
  capturedAt: string
}

let cached: SmokeContext | null = null

export function smokeContext(): SmokeContext {
  if (cached) return cached
  const raw = readFileSync(path.join(__dirname, '..', '.smoke-context.json'), 'utf-8')
  cached = JSON.parse(raw) as SmokeContext
  return cached
}

// 考勤：勞基法 5 年保存期擋 DELETE（>= today-5y 不可刪）。
// 用 2018-06-15（已過保存期、非月初/月底避封存 edge case）。
// 若未來 today > 2023-06-15 也仍 OK——5y cutoff 是 today-5y，2018 永遠 < cutoff。
export function preRetentionDate(): string {
  return '2018-06-15'
}

// 假單：BE 自動排除週末+國定假日；用未來 Monday 確保 leave_hours=8 對得上。
// 2027-01-18 是 Monday，且非 Taiwan 國定假日（CNY 2027 為 2/6）。
export function futureWeekday(offsetDays = 0): string {
  const base = new Date('2027-01-18T00:00:00Z')
  base.setUTCDate(base.getUTCDate() + offsetDays)
  return base.toISOString().slice(0, 10)
}

export function previousMonthYM(): { year: number; month: number } {
  const now = new Date()
  const y = now.getUTCFullYear()
  const m = now.getUTCMonth()
  if (m === 0) return { year: y - 1, month: 12 }
  return { year: y, month: m }
}
