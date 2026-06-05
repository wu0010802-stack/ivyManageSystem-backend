import { request } from '@playwright/test'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const STORAGE_STATE_PATH = path.join(__dirname, '..', 'storage-state.json')

function readEnv(name: string): string {
  const value = process.env[name]
  if (!value || value.trim() === '') {
    throw new Error(
      `[e2e/globalSetup] 缺少必要環境變數 ${name}。請先在 e2e/.env 設值（範本見 e2e/.env.example），` +
        '並用 `set -a; . ./.env; set +a` 載入後再跑 npm test。',
    )
  }
  return value
}

export default async function globalSetup(): Promise<void> {
  const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:5173'
  const apiURL = process.env.E2E_API_URL ?? baseURL
  const username = readEnv('E2E_ADMIN_USERNAME')
  const password = readEnv('E2E_ADMIN_PASSWORD')
  const targetEmployeeId = Number(readEnv('E2E_TEST_EMPLOYEE_ID'))
  if (!Number.isFinite(targetEmployeeId) || targetEmployeeId <= 0) {
    throw new Error(
      `[e2e/globalSetup] E2E_TEST_EMPLOYEE_ID 必須是正整數，目前值: ${process.env.E2E_TEST_EMPLOYEE_ID}`,
    )
  }

  // 後端 CSRF middleware 要求 state-changing 請求帶 allowlist 內的 Origin（login 是 POST）。
  const ctx = await request.newContext({ baseURL: apiURL, extraHTTPHeaders: { Origin: baseURL } })
  let loginRes
  try {
    loginRes = await ctx.post('/api/auth/login', {
      data: { username, password },
      headers: { 'Content-Type': 'application/json' },
    })
  } catch (err) {
    throw new Error(
      `[e2e/globalSetup] 連不到後端 (${apiURL}/api/auth/login)。請確認 start.sh 已啟動兩端 dev server。\n原始錯誤: ${(err as Error).message}`,
    )
  }
  if (!loginRes.ok()) {
    throw new Error(
      `[e2e/globalSetup] admin 登入失敗 (status=${loginRes.status()}): ${await loginRes.text()}。` +
        '請確認 E2E_ADMIN_USERNAME/PASSWORD 正確、帳號 role=admin、且未觸發 login rate limit（重啟後端可清）。',
    )
  }
  const loginBody = (await loginRes.json()) as { user?: { role?: string; employee_id?: number; id?: number } }
  if (loginBody.user?.role !== 'admin') {
    throw new Error(
      `[e2e/globalSetup] 帳號 ${username} role=${loginBody.user?.role} 非 admin，無法跑 admin smoke。`,
    )
  }
  const adminEmployeeId = loginBody.user?.employee_id ?? null
  if (adminEmployeeId === targetEmployeeId) {
    throw new Error(
      `[e2e/globalSetup] E2E_TEST_EMPLOYEE_ID (${targetEmployeeId}) 不可為 admin 自身的 employee_id (${adminEmployeeId})。` +
        'attendance/leave 端點有 self-guard，會 422。請改指向另一個非時薪、在職員工。',
    )
  }

  const empRes = await ctx.get(`/api/employees/${targetEmployeeId}`)
  if (!empRes.ok()) {
    throw new Error(
      `[e2e/globalSetup] 取不到 employee id=${targetEmployeeId} (status=${empRes.status()})。請確認 E2E_TEST_EMPLOYEE_ID 對應到實際存在的員工。`,
    )
  }
  const emp = (await empRes.json()) as { employee_type?: string; is_active?: boolean; name?: string }
  if (emp.employee_type === 'hourly') {
    throw new Error(
      `[e2e/globalSetup] 測試員工 id=${targetEmployeeId} (${emp.name}) 為時薪制，salary simulate 會 422。請換成月薪員工。`,
    )
  }
  if (emp.is_active === false) {
    throw new Error(
      `[e2e/globalSetup] 測試員工 id=${targetEmployeeId} (${emp.name}) 為離職狀態，不適合 smoke。請換成在職員工。`,
    )
  }

  // 注意：此 storageState 只含 httpOnly cookie（API 認證足夠），但**不含**前端判定
  // 登入態的 localStorage['userInfo']。瀏覽器頁面 spec 因此 getUserInfo()===null 會被
  // router guard 導去 /login。曾試過注入 userInfo 讓瀏覽器真登入，但與後端 staff-refresh
  // -rotation 衝突（頁面載入觸發 refresh → 輪替 token + bump token_version → 共用的
  // storageState cookie 失效 → 後續 API spec 401）。真正修法需 e2e harness 重設計
  // （每 spec 獨立認證 / 測試環境關輪替），列為 follow-up（見 offboarding.spec.ts fixme）。
  await ctx.storageState({ path: STORAGE_STATE_PATH })
  await fs.writeFile(
    path.join(__dirname, '..', '.smoke-context.json'),
    JSON.stringify(
      {
        adminUsername: username,
        adminEmployeeId,
        targetEmployeeId,
        targetEmployeeName: emp.name,
        apiURL,
        baseURL,
        capturedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  )
  await ctx.dispose()

  console.log(
    `[e2e/globalSetup] admin=${username} (emp ${adminEmployeeId}) → target=${emp.name} (emp ${targetEmployeeId}); storage saved`,
  )
}
