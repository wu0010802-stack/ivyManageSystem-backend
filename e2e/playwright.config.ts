import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173'
const STORAGE_STATE = path.join(__dirname, 'storage-state.json')

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list'], ['html', { open: 'never' }]],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  globalSetup: path.join(__dirname, 'fixtures', 'globalSetup.ts'),

  use: {
    baseURL: BASE_URL,
    // 後端 CSRF middleware（middleware/csrf_origin.py）對 state-changing 方法要求 Origin 在 allowlist。
    // request fixture 與瀏覽器 context 皆套此 header，否則 POST/PUT/DELETE → 403。
    extraHTTPHeaders: { Origin: BASE_URL },
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },

  projects: [
    {
      name: 'unauth',
      testMatch: /health\.spec\.ts|admin-login\.spec\.ts/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'auth-admin',
      testIgnore: /health\.spec\.ts|admin-login\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        storageState: STORAGE_STATE,
      },
    },
  ],
})

export const STORAGE_STATE_PATH = STORAGE_STATE
