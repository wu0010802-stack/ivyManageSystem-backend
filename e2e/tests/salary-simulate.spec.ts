import { test, expect } from '@playwright/test'
import { smokeContext, previousMonthYM } from '../fixtures/smokeContext'

test.describe('@smoke salary simulate', () => {
  test('admin 對測試員工跑 /salaries/simulate（不寫 DB）→ 回傳含 gross_salary/net_pay', async ({
    request,
  }) => {
    const ctx = smokeContext()
    const { year, month } = previousMonthYM()

    const res = await request.post('/api/salaries/simulate', {
      data: {
        employee_id: ctx.targetEmployeeId,
        year,
        month,
        overrides: {},
      },
    })
    expect(
      res.ok(),
      `POST /salaries/simulate 失敗: ${res.status()} ${await res.text()}`,
    ).toBeTruthy()

    const body = (await res.json()) as {
      employee?: { id?: number }
      simulated?: { base_salary?: unknown; gross_salary?: unknown; net_pay?: unknown }
    }
    expect(body.employee?.id, '回傳 employee.id 應等於請求對象').toBe(ctx.targetEmployeeId)
    expect(body.simulated, '回傳應含 simulated 區塊').toBeTruthy()
    expect(typeof body.simulated?.base_salary, 'base_salary 應為 number').toBe('number')
    expect(typeof body.simulated?.gross_salary, 'gross_salary 應為 number').toBe('number')
    expect(typeof body.simulated?.net_pay, 'net_pay 應為 number').toBe('number')
    // base_salary 不應為 0/空——驗證 salary engine 有真的吃到員工底薪
    expect(body.simulated?.base_salary, 'base_salary 應 > 0（engine 有讀到員工底薪）').toBeTruthy()
  })
})
