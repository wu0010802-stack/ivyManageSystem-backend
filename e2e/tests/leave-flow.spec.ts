import { test, expect } from '@playwright/test'
import { smokeContext, futureWeekday } from '../fixtures/smokeContext'

test.describe('@smoke leave submit + approve', () => {
  test('admin 替測試員工建假單 → 簽核 → 清除', async ({ request }) => {
    const ctx = smokeContext()
    // 用平日否則 BE 算 work hours=0 → 422
    const startDate = futureWeekday(0)
    const endDate = futureWeekday(0)

    const created = await request.post('/api/leaves', {
      data: {
        employee_id: ctx.targetEmployeeId,
        leave_type: 'personal',
        start_date: startDate,
        end_date: endDate,
        leave_hours: 8,
        reason: `[e2e smoke ${new Date().toISOString()}]`,
      },
    })
    expect(
      created.ok(),
      `POST /leaves 失敗: ${created.status()} ${await created.text()}`,
    ).toBeTruthy()
    const leave = (await created.json()) as { id: number }
    expect(leave.id).toBeGreaterThan(0)

    let cleanedUp = false
    try {
      const approve = await request.put(`/api/leaves/${leave.id}/approve`, {
        data: { approved: true },
      })
      expect(
        approve.ok(),
        `PUT /leaves/${leave.id}/approve 失敗: ${approve.status()} ${await approve.text()}`,
      ).toBeTruthy()

      // BE 無 GET /leaves/{id}，改用 list+filter 確認 approve 持久化
      const listed = await request.get(
        `/api/leaves?employee_id=${ctx.targetEmployeeId}&start_date=${startDate}&end_date=${endDate}`,
      )
      expect(listed.ok(), `查詢假單失敗: ${listed.status()} ${await listed.text()}`).toBeTruthy()
      // 審核狀態自 approval-status-enum 遷移後改用 status/approval_status（'approved'），
      // 舊的 is_approved 布林已移除；容錯查多欄位避免再次因欄位改名而脆弱。
      const items = (await listed.json()) as Array<{
        id?: number
        status?: string
        approval_status?: string
        is_approved?: boolean
      }>
      const hit = items.find((r) => r.id === leave.id)
      expect(hit, `查不到剛建的假單 id=${leave.id}`).toBeTruthy()
      const approved =
        hit?.status === 'approved' ||
        hit?.approval_status === 'approved' ||
        hit?.is_approved === true
      expect(approved, `假單未顯示已核准: ${JSON.stringify(hit)}`).toBe(true)
    } finally {
      const del = await request.delete(`/api/leaves/${leave.id}`)
      cleanedUp = del.ok() || del.status() === 404
      expect(
        cleanedUp,
        `cleanup DELETE /leaves/${leave.id} 失敗: ${del.status()} ${await del.text()}`,
      ).toBeTruthy()
    }
  })
})
