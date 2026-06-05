import { test, expect } from '@playwright/test'
import { smokeContext, preRetentionDate } from '../fixtures/smokeContext'

test.describe('@smoke attendance mutation', () => {
  test('admin 為測試員工寫一筆考勤 → 查得到 → 清除', async ({ request }) => {
    const ctx = smokeContext()
    // 用過保存期日期才能 DELETE 清除（勞基法 5 年保存期）
    const date = preRetentionDate()
    const employeeId = ctx.targetEmployeeId

    const created = await request.post('/api/attendance/record', {
      data: {
        employee_id: employeeId,
        date,
        punch_in: '08:00',
        punch_out: '17:00',
      },
    })
    expect(created.ok(), `POST /attendance/record 失敗: ${created.status()} ${await created.text()}`).toBeTruthy()

    try {
      const [year, month] = date.split('-').map(Number)
      const list = await request.get(
        `/api/attendance/records?employee_id=${employeeId}&year=${year}&month=${month}`,
      )
      expect(list.ok(), `查詢考勤失敗: ${list.status()} ${await list.text()}`).toBeTruthy()
      const items = (await list.json()) as Array<{ date?: string; attendance_date?: string }>
      const hit = items.find((r) => (r.date ?? r.attendance_date)?.startsWith(date))
      expect(hit, `查不到剛建的 ${date} 考勤紀錄（員工 ${employeeId}）`).toBeTruthy()
    } finally {
      const del = await request.delete(`/api/attendance/record/${employeeId}/${date}`)
      expect(
        del.ok() || del.status() === 404,
        `cleanup DELETE 失敗: ${del.status()} ${await del.text()}`,
      ).toBeTruthy()
    }
  })
})
