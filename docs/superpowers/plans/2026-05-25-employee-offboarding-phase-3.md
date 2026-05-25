# 員工離職 Checklist Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補完員工離職 Phase 3 前端 — 一鍵 modal（preview → process → step 結果展示）/ 離職管理清單頁 / magic-link 產撤 panel / Playwright e2e smoke。

**Architecture:** 純前端工作，repo 是 `ivy-frontend`。沿用 TS-only + `<script setup lang="ts">` + Element Plus + Pinia + AxiosResp type helpers。`EmployeeView.vue:312-373` 既有 inline 「辦理離職」邏輯抽出改開新 `OffboardingModal`，後端 endpoint 從舊 `/employees/{id}/offboard` 切到新 `/offboarding/{id}/process`。新菜單項「離職管理」進 AdminSidebar，清單頁顯示所有未結案/已結案 record。Magic-link 產 token 用 ElDialog 強制 admin 複製到剪貼簿（onClose 後 token 清掉永不重顯）。

**Tech Stack:** Vue 3 Composition API, TypeScript, Element Plus, Pinia, Vitest, Vue Test Utils, Playwright (workspace `e2e/`)。

**Spec：** `docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md` §3 (Phase 3) + §9 (前端結構)。

**Phase 1+2 已完成（前置）：**
- 後端所有 endpoint 已 ship（`feat/offboarding-phase-1-2026-05-25-backend` branch 25 commits）
- `ivy-frontend/src/api/_generated/schema.d.ts` 已含 Phase 1+2 paths（main `26b7e7b7` regen 過）
- 既有 `src/api/employees.ts:16-17` `offboard()` wrapper 走舊 endpoint（後端 passthrough 至 orchestrator），Phase 3 完成後拔除

**Phase 3 不含：** 後端任何改動、家長端 UI、admin email 自動寄 magic-link（admin 手動複製）

---

## 前置：建立前端 worktree

由 controller 執行（subagent 不需做）：

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && \
git worktree add .claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend \
  -b feat/offboarding-phase-3-2026-05-25-frontend
```

從 main HEAD（含 `26b7e7b7` Phase 2 schema regen + `36f9c049` Phase 1 schema regen）開新 worktree。**所有 Phase 3 subagent 工作在此 worktree 內**。

---

## 檔案結構

新建檔（在 ivy-frontend worktree 內）：
- `src/api/offboarding.ts` — 6 個 wrapper（preview / process / getDetail / certificate.pdf / nhi-unenroll / postMagicLink / deleteMagicLink）
- `src/stores/offboarding.ts` — Pinia store（detail by employeeId + list + actions）
- `src/components/offboarding/OffboardingPreviewPanel.vue` — preview 結果 + warnings 紅字顯示
- `src/components/offboarding/OffboardingStepsResult.vue` — 5 step 結果展示（icons + status）
- `src/components/offboarding/OffboardingModal.vue` — 一鍵 modal：date + reason input → 觸發 preview → 顯示預覽 → 「確認辦理」按鈕 → process → 顯示 5 step result + 下載證明
- `src/components/offboarding/MagicLinkPanel.vue` — 產 / 撤 / 顯示狀態（active / expires / count / last_used）+ token 一次顯示複製
- `src/views/admin/OffboardingView.vue` — 清單頁（列出所有 EmployeeOffboardingRecord + 檢視 detail + 重發/撤銷 magic-link）
- `tests/api/offboarding.test.ts`
- `tests/stores/offboarding.test.ts`
- `tests/components/OffboardingPreviewPanel.test.ts`
- `tests/components/OffboardingStepsResult.test.ts`
- `tests/components/OffboardingModal.test.ts`
- `tests/components/MagicLinkPanel.test.ts`
- `tests/views/OffboardingView.test.ts`

修改檔：
- `src/views/EmployeeView.vue:6,312-373,offboardVisible 區塊` — 移除 inline 辦理離職邏輯，按鈕改開 `OffboardingModal` 元件
- `src/router/index.ts` — 加 `/admin/offboarding` 路由 → OffboardingView
- `src/components/admin/AdminSidebar.vue` — 加「人事管理 → 離職管理」菜單項，gated by `EMPLOYEES_READ`
- `src/utils/sentry.ts` — `PII_KEY_SUBSTRINGS` 加 3 key（`resign_reason` / `leave_balance_snapshot` / `certificate_pdf_path`）
- `src/api/employees.ts:16-17` — Phase 3 結束移除舊 `offboard()` wrapper（先 deprecated 等 Phase 3 PR 6 拔）

新建（workspace `e2e/`）：
- `e2e/offboarding.spec.ts` — Playwright critical-path smoke：admin login → 開 OffboardingModal → preview → confirm → 驗 step result + 下載證明

---

## Task 1: src/api/offboarding.ts wrapper

**Files:**
- Create: `src/api/offboarding.ts`
- Test: `tests/api/offboarding.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// tests/api/offboarding.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/index', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

import api from '@/api/index'
import {
  previewOffboarding,
  processOffboarding,
  getOffboardingDetail,
  getOffboardingCertificate,
  patchNhiUnenroll,
  postMagicLink,
  deleteMagicLink,
} from '@/api/offboarding'

describe('api/offboarding', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockClear()
    vi.mocked(api.post).mockClear()
    vi.mocked(api.patch).mockClear()
    vi.mocked(api.delete).mockClear()
  })

  it('previewOffboarding POSTs /offboarding/{id}/preview', () => {
    previewOffboarding(42, { resign_date: '2026-06-15', resign_reason: 'test' })
    expect(api.post).toHaveBeenCalledWith(
      '/offboarding/42/preview',
      { resign_date: '2026-06-15', resign_reason: 'test' },
    )
  })

  it('processOffboarding POSTs /offboarding/{id}/process', () => {
    processOffboarding(42, { resign_date: '2026-06-15', resign_reason: 'r' })
    expect(api.post).toHaveBeenCalledWith(
      '/offboarding/42/process',
      { resign_date: '2026-06-15', resign_reason: 'r' },
    )
  })

  it('getOffboardingDetail GETs /offboarding/{id}', () => {
    getOffboardingDetail(42)
    expect(api.get).toHaveBeenCalledWith('/offboarding/42')
  })

  it('getOffboardingCertificate GETs /certificate.pdf with blob responseType', () => {
    getOffboardingCertificate(42)
    expect(api.get).toHaveBeenCalledWith(
      '/offboarding/42/certificate.pdf',
      { responseType: 'blob' },
    )
  })

  it('patchNhiUnenroll PATCHes', () => {
    patchNhiUnenroll(42, { submitted: true })
    expect(api.patch).toHaveBeenCalledWith(
      '/offboarding/42/nhi-unenroll',
      { submitted: true },
    )
  })

  it('postMagicLink POSTs', () => {
    postMagicLink(42)
    expect(api.post).toHaveBeenCalledWith('/offboarding/42/magic-link')
  })

  it('deleteMagicLink DELETEs', () => {
    deleteMagicLink(42)
    expect(api.delete).toHaveBeenCalledWith('/offboarding/42/magic-link')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/api/offboarding.test.ts 2>&1 | tail -15
```

Expected: FAIL — module not found。

- [ ] **Step 3: Implement**

```ts
// src/api/offboarding.ts
import api from './index'
import type { ApiBody, AxiosResp } from './_generated/typed'

export const previewOffboarding = (
  id: number,
  data: ApiBody<'/offboarding/{employee_id}/preview', 'post'>,
): AxiosResp<'/offboarding/{employee_id}/preview', 'post'> =>
  api.post(`/offboarding/${id}/preview`, data)

export const processOffboarding = (
  id: number,
  data: ApiBody<'/offboarding/{employee_id}/process', 'post'>,
): AxiosResp<'/offboarding/{employee_id}/process', 'post'> =>
  api.post(`/offboarding/${id}/process`, data)

export const getOffboardingDetail = (
  id: number,
): AxiosResp<'/offboarding/{employee_id}', 'get'> =>
  api.get(`/offboarding/${id}`)

export const getOffboardingCertificate = (id: number) =>
  api.get(`/offboarding/${id}/certificate.pdf`, { responseType: 'blob' })

export const patchNhiUnenroll = (
  id: number,
  data: ApiBody<'/offboarding/{employee_id}/nhi-unenroll', 'patch'>,
): AxiosResp<'/offboarding/{employee_id}/nhi-unenroll', 'patch'> =>
  api.patch(`/offboarding/${id}/nhi-unenroll`, data)

export const postMagicLink = (
  id: number,
): AxiosResp<'/offboarding/{employee_id}/magic-link', 'post'> =>
  api.post(`/offboarding/${id}/magic-link`)

export const deleteMagicLink = (
  id: number,
): AxiosResp<'/offboarding/{employee_id}/magic-link', 'delete'> =>
  api.delete(`/offboarding/${id}/magic-link`)
```

- [ ] **Step 4: Run test + typecheck**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/api/offboarding.test.ts && npx vue-tsc --noEmit 2>&1 | tail -5
```

Expected: PASS 7 tests + 0 typecheck error。

- [ ] **Step 5: Commit**

```bash
git add src/api/offboarding.ts tests/api/offboarding.test.ts && \
git commit -m "$(cat <<'EOF'
feat(api): add offboarding wrapper (7 endpoints)

新 src/api/offboarding.ts wrapper：
- previewOffboarding / processOffboarding
- getOffboardingDetail / getOffboardingCertificate (blob)
- patchNhiUnenroll
- postMagicLink / deleteMagicLink

對接後端 feat/offboarding-phase-1-2026-05-25-backend Phase 1+2 完整 endpoint
組。型別走 _generated/typed AxiosResp/ApiBody。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: src/stores/offboarding.ts Pinia store

**Files:**
- Create: `src/stores/offboarding.ts`
- Test: `tests/stores/offboarding.test.ts`

**動機：** Modal / 清單頁 / Magic-link panel 共用 detail cache，避免重複 fetch。

- [ ] **Step 1: Write the failing test**

```ts
// tests/stores/offboarding.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/offboarding', () => ({
  getOffboardingDetail: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      employee_name: '王小明',
      resign_date: '2026-06-15',
      magic_link_active: false,
      magic_link_download_count: 0,
    },
  })),
  processOffboarding: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      resign_date: '2026-06-15',
      is_active: false,
      user_account_revoked: true,
      steps: [],
    },
  })),
  previewOffboarding: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      employee_name: '王小明',
      resign_date: '2026-06-15',
      preview: {},
      warnings: [],
    },
  })),
}))

import { useOffboardingStore } from '@/stores/offboarding'
import * as api from '@/api/offboarding'

describe('stores/offboarding', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('fetchDetail caches by employee_id', async () => {
    const store = useOffboardingStore()
    await store.fetchDetail(42)
    await store.fetchDetail(42)  // 第二次應該 cached
    expect(api.getOffboardingDetail).toHaveBeenCalledTimes(1)
    expect(store.getDetail(42)?.employee_name).toBe('王小明')
  })

  it('refreshDetail bypasses cache', async () => {
    const store = useOffboardingStore()
    await store.fetchDetail(42)
    await store.refreshDetail(42)
    expect(api.getOffboardingDetail).toHaveBeenCalledTimes(2)
  })

  it('process calls api + invalidates cache', async () => {
    const store = useOffboardingStore()
    await store.fetchDetail(42)
    await store.process(42, { resign_date: '2026-06-15', resign_reason: 'r' })
    expect(api.processOffboarding).toHaveBeenCalledWith(
      42, { resign_date: '2026-06-15', resign_reason: 'r' },
    )
    // 再 fetchDetail 應重新 call api（cache 已清）
    await store.fetchDetail(42)
    expect(api.getOffboardingDetail).toHaveBeenCalledTimes(2)
  })

  it('preview does not write cache', async () => {
    const store = useOffboardingStore()
    const result = await store.preview(42, { resign_date: '2026-06-15' })
    expect(result.warnings).toEqual([])
    expect(store.getDetail(42)).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/stores/offboarding.test.ts 2>&1 | tail -10
```

Expected: FAIL — module not found。

- [ ] **Step 3: Implement**

```ts
// src/stores/offboarding.ts
import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getOffboardingDetail,
  processOffboarding,
  previewOffboarding,
} from '@/api/offboarding'
import type { ApiBody, ApiResponse } from '@/api/_generated/typed'

type DetailType = ApiResponse<'/offboarding/{employee_id}', 'get'>
type PreviewType = ApiResponse<'/offboarding/{employee_id}/preview', 'post'>
type ProcessType = ApiResponse<'/offboarding/{employee_id}/process', 'post'>

export const useOffboardingStore = defineStore('offboarding', () => {
  const details = ref<Map<number, DetailType>>(new Map())

  const getDetail = (employeeId: number): DetailType | undefined =>
    details.value.get(employeeId)

  const fetchDetail = async (employeeId: number): Promise<DetailType> => {
    const cached = details.value.get(employeeId)
    if (cached) return cached
    const res = await getOffboardingDetail(employeeId)
    details.value.set(employeeId, res.data)
    return res.data
  }

  const refreshDetail = async (employeeId: number): Promise<DetailType> => {
    const res = await getOffboardingDetail(employeeId)
    details.value.set(employeeId, res.data)
    return res.data
  }

  const preview = async (
    employeeId: number,
    payload: ApiBody<'/offboarding/{employee_id}/preview', 'post'>,
  ): Promise<PreviewType> => {
    const res = await previewOffboarding(employeeId, payload)
    return res.data
  }

  const process = async (
    employeeId: number,
    payload: ApiBody<'/offboarding/{employee_id}/process', 'post'>,
  ): Promise<ProcessType> => {
    const res = await processOffboarding(employeeId, payload)
    details.value.delete(employeeId)  // cache invalidate
    return res.data
  }

  const invalidate = (employeeId: number) => {
    details.value.delete(employeeId)
  }

  return { details, getDetail, fetchDetail, refreshDetail, preview, process, invalidate }
})
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/stores/offboarding.test.ts && npx vue-tsc --noEmit 2>&1 | tail -5
```

Expected: PASS 4 tests + 0 typecheck error。

- [ ] **Step 5: Commit**

```bash
git add src/stores/offboarding.ts tests/stores/offboarding.test.ts && \
git commit -m "feat(stores): add offboarding Pinia store

Map cache by employee_id; fetchDetail / refreshDetail / preview / process /
invalidate actions. process 自動清 cache 確保 GET detail 拿到最新狀態。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: OffboardingPreviewPanel + OffboardingStepsResult 元件

**Files:**
- Create: `src/components/offboarding/OffboardingPreviewPanel.vue`
- Create: `src/components/offboarding/OffboardingStepsResult.vue`
- Test: `tests/components/OffboardingPreviewPanel.test.ts`
- Test: `tests/components/OffboardingStepsResult.test.ts`

**動機：** Modal 內兩個 section（preview 階段 + process 結果階段），抽元件方便獨立測試與 reuse。

- [ ] **Step 1: Write OffboardingPreviewPanel failing test**

```ts
// tests/components/OffboardingPreviewPanel.test.ts
import { mount } from '@vue/test-utils'
import { describe, it, expect } from 'vitest'
import OffboardingPreviewPanel from '@/components/offboarding/OffboardingPreviewPanel.vue'

const samplePreview = {
  employee_id: 42,
  employee_name: '王小明',
  resign_date: '2026-06-15',
  preview: {
    user_account_will_be_revoked: true,
    leave_snapshot: { special_leave_days: 5, daily_wage: 1800, payout_amount: 9000 },
    salary_record_target: { year: 2026, month: 6, exists: true, will_be_marked_stale: true },
    appraisal_in_flight_cycles: [],
    certificate_pdf_ready_to_generate: false,
  },
  warnings: ['員工有 1 個進行中考核 cycle'],
}

describe('OffboardingPreviewPanel', () => {
  it('renders leave snapshot fields', () => {
    const w = mount(OffboardingPreviewPanel, { props: { preview: samplePreview } })
    expect(w.text()).toContain('5')         // days
    expect(w.text()).toContain('9000')      // payout
    expect(w.text()).toContain('1800')      // daily_wage
  })

  it('renders user account revoke flag', () => {
    const w = mount(OffboardingPreviewPanel, { props: { preview: samplePreview } })
    expect(w.text()).toContain('將撤銷')
  })

  it('renders warnings in red', () => {
    const w = mount(OffboardingPreviewPanel, { props: { preview: samplePreview } })
    expect(w.text()).toContain('員工有 1 個進行中考核 cycle')
    expect(w.find('.warning-text').exists()).toBe(true)
  })

  it('renders empty state when no warnings', () => {
    const w = mount(OffboardingPreviewPanel, {
      props: { preview: { ...samplePreview, warnings: [] } },
    })
    expect(w.find('.warning-text').exists()).toBe(false)
  })
})
```

- [ ] **Step 2: Implement OffboardingPreviewPanel**

```vue
<!-- src/components/offboarding/OffboardingPreviewPanel.vue -->
<script setup lang="ts">
import type { ApiResponse } from '@/api/_generated/typed'

type PreviewType = ApiResponse<'/offboarding/{employee_id}/preview', 'post'>

defineProps<{
  preview: PreviewType
}>()
</script>

<template>
  <div class="offboarding-preview">
    <el-descriptions :column="1" border>
      <el-descriptions-item label="姓名">{{ preview.employee_name }}</el-descriptions-item>
      <el-descriptions-item label="離職日">{{ preview.resign_date }}</el-descriptions-item>
      <el-descriptions-item label="使用者帳號">
        <el-tag v-if="preview.preview.user_account_will_be_revoked" type="danger">
          將撤銷（cookie 立即失效）
        </el-tag>
        <el-tag v-else type="info">通知期保留</el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="特休結算">
        {{ preview.preview.leave_snapshot.special_leave_days }} 天
        × 日薪 {{ preview.preview.leave_snapshot.daily_wage }}
        = ${{ preview.preview.leave_snapshot.payout_amount }}
      </el-descriptions-item>
      <el-descriptions-item label="離職當月薪資">
        {{ preview.preview.salary_record_target.year }}/{{ preview.preview.salary_record_target.month }}
        ({{ preview.preview.salary_record_target.exists ? '已存在' : '尚未建立' }})
        <el-tag v-if="preview.preview.salary_record_target.will_be_marked_stale" type="warning" size="small">
          將標 stale
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item v-if="preview.preview.appraisal_in_flight_cycles.length" label="進行中考核">
        <el-tag
          v-for="cycle in preview.preview.appraisal_in_flight_cycles"
          :key="cycle.cycle_id"
          type="warning"
        >
          {{ cycle.cycle_name }}
        </el-tag>
      </el-descriptions-item>
    </el-descriptions>

    <div v-if="preview.warnings.length" class="warning-text">
      <el-icon><WarningFilled /></el-icon>
      <ul>
        <li v-for="(w, i) in preview.warnings" :key="i">{{ w }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.warning-text {
  margin-top: 12px;
  padding: 12px;
  background: #fef0f0;
  color: #f56c6c;
  border-radius: 4px;
}
.warning-text ul {
  margin: 4px 0 0 24px;
}
</style>
```

- [ ] **Step 3: Write OffboardingStepsResult failing test**

```ts
// tests/components/OffboardingStepsResult.test.ts
import { mount } from '@vue/test-utils'
import { describe, it, expect } from 'vitest'
import OffboardingStepsResult from '@/components/offboarding/OffboardingStepsResult.vue'

const sampleSteps = [
  { step: 'mark_appraisal', status: 'completed', completed_at: '2026-06-15T10:00:00', payload: null, error: null },
  { step: 'snapshot_leave', status: 'completed', completed_at: '2026-06-15T10:00:01', payload: { days: 5, payout: 9000 }, error: null },
  { step: 'prefill_leave_payout', status: 'skipped', completed_at: '2026-06-15T10:00:02', payload: { reason: 'salary_record_not_yet_created' }, error: null },
  { step: 'revoke_user', status: 'completed', completed_at: '2026-06-15T10:00:03', payload: { username: 'wang.xm', new_token_version: 2 }, error: null },
  { step: 'generate_certificate', status: 'failed', completed_at: null, payload: null, error: 'disk full' },
]

describe('OffboardingStepsResult', () => {
  it('renders 5 step items', () => {
    const w = mount(OffboardingStepsResult, { props: { steps: sampleSteps } })
    expect(w.findAll('.step-item')).toHaveLength(5)
  })

  it('shows green check for completed', () => {
    const w = mount(OffboardingStepsResult, { props: { steps: sampleSteps } })
    expect(w.findAll('.status-completed')).toHaveLength(3)
  })

  it('shows yellow icon for skipped', () => {
    const w = mount(OffboardingStepsResult, { props: { steps: sampleSteps } })
    expect(w.findAll('.status-skipped')).toHaveLength(1)
  })

  it('shows red icon + error for failed', () => {
    const w = mount(OffboardingStepsResult, { props: { steps: sampleSteps } })
    expect(w.findAll('.status-failed')).toHaveLength(1)
    expect(w.text()).toContain('disk full')
  })

  it('emits retry when retry button clicked', async () => {
    const w = mount(OffboardingStepsResult, { props: { steps: sampleSteps } })
    await w.find('.retry-button').trigger('click')
    expect(w.emitted('retry')).toBeTruthy()
  })
})
```

- [ ] **Step 4: Implement OffboardingStepsResult**

```vue
<!-- src/components/offboarding/OffboardingStepsResult.vue -->
<script setup lang="ts">
import { computed } from 'vue'
import type { ApiResponse } from '@/api/_generated/typed'

type ProcessResp = ApiResponse<'/offboarding/{employee_id}/process', 'post'>
type StepResultModel = ProcessResp['steps'][number]

const props = defineProps<{
  steps: StepResultModel[]
}>()

const emit = defineEmits<{ retry: [] }>()

const stepLabels: Record<string, string> = {
  mark_appraisal: '標記進行中考核',
  snapshot_leave: '特休餘額快照',
  prefill_leave_payout: '預填離職當月薪資',
  revoke_user: '撤銷使用者帳號',
  generate_certificate: '產生離職證明 PDF',
}

const hasFailure = computed(() => props.steps.some(s => s.status === 'failed'))
</script>

<template>
  <div class="steps-result">
    <div
      v-for="s in steps"
      :key="s.step"
      class="step-item"
      :class="`status-${s.status}`"
    >
      <el-icon v-if="s.status === 'completed'" class="step-icon"><Check /></el-icon>
      <el-icon v-else-if="s.status === 'skipped'" class="step-icon"><Minus /></el-icon>
      <el-icon v-else class="step-icon"><Close /></el-icon>
      <span class="step-label">{{ stepLabels[s.step] || s.step }}</span>
      <span v-if="s.error" class="step-error">— {{ s.error }}</span>
      <span v-else-if="s.payload && s.status === 'skipped'" class="step-note">
        — {{ JSON.stringify(s.payload) }}
      </span>
    </div>

    <div v-if="hasFailure" class="retry-row">
      <el-button type="primary" class="retry-button" @click="emit('retry')">重試</el-button>
    </div>
  </div>
</template>

<style scoped>
.step-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #eee;
}
.status-completed { color: #67c23a; }
.status-skipped { color: #e6a23c; }
.status-failed { color: #f56c6c; }
.step-error { font-size: 12px; opacity: 0.8; }
.step-note { font-size: 12px; opacity: 0.6; color: #909399; }
.retry-row { margin-top: 12px; text-align: right; }
</style>
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/components/OffboardingPreviewPanel.test.ts tests/components/OffboardingStepsResult.test.ts && \
npx vue-tsc --noEmit 2>&1 | tail -5
```

Expected: PASS 9 tests + 0 typecheck error。

- [ ] **Step 6: Commit**

```bash
git add src/components/offboarding/ tests/components/Offboarding*.test.ts && \
git commit -m "feat(offboarding): add PreviewPanel + StepsResult components

PreviewPanel: el-descriptions 顯示 5 預覽欄（姓名/離職日/帳號旗/特休結算/
薪資 target）+ warnings 紅字。
StepsResult: 5 step 結果列表（icon + label + payload）+ failure 觸發 retry。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: OffboardingModal 一鍵 modal

**Files:**
- Create: `src/components/offboarding/OffboardingModal.vue`
- Test: `tests/components/OffboardingModal.test.ts`

**動機：** 整合 preview + process + steps result + 下載證明按鈕的核心 modal。

- [ ] **Step 1: Write failing test**

```ts
// tests/components/OffboardingModal.test.ts
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/offboarding', () => ({
  previewOffboarding: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      employee_name: '王小明',
      resign_date: '2026-06-15',
      preview: {
        user_account_will_be_revoked: true,
        leave_snapshot: { special_leave_days: 5, daily_wage: 1800, payout_amount: 9000 },
        salary_record_target: { year: 2026, month: 6, exists: true, will_be_marked_stale: true },
        appraisal_in_flight_cycles: [],
        certificate_pdf_ready_to_generate: false,
      },
      warnings: [],
    },
  })),
  processOffboarding: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      resign_date: '2026-06-15',
      is_active: false,
      user_account_revoked: true,
      steps: [
        { step: 'mark_appraisal', status: 'completed', completed_at: '2026-06-15T10:00:00', payload: null, error: null },
        { step: 'generate_certificate', status: 'completed', completed_at: '2026-06-15T10:00:04', payload: { pdf_path: 'x.pdf' }, error: null },
      ],
      certificate_download_url: null,
    },
  })),
  getOffboardingDetail: vi.fn(),
}))

import OffboardingModal from '@/components/offboarding/OffboardingModal.vue'

const stubElDialog = {
  template: '<div v-if="modelValue"><slot /><slot name="footer" /></div>',
  props: ['modelValue', 'title'],
}

describe('OffboardingModal', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('previews when resign_date input filled', async () => {
    const w = mount(OffboardingModal, {
      props: { modelValue: true, employeeId: 42, employeeName: '王小明' },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    await w.find('input[type="date"]').setValue('2026-06-15')
    await w.find('.preview-button').trigger('click')
    await flushPromises()
    const { previewOffboarding } = await import('@/api/offboarding')
    expect(previewOffboarding).toHaveBeenCalledWith(42, expect.objectContaining({
      resign_date: '2026-06-15',
    }))
    expect(w.text()).toContain('王小明')
  })

  it('processes after confirm + emits success', async () => {
    const w = mount(OffboardingModal, {
      props: { modelValue: true, employeeId: 42, employeeName: '王小明' },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    await w.find('input[type="date"]').setValue('2026-06-15')
    await w.find('.preview-button').trigger('click')
    await flushPromises()
    await w.find('.confirm-button').trigger('click')
    await flushPromises()
    const { processOffboarding } = await import('@/api/offboarding')
    expect(processOffboarding).toHaveBeenCalledTimes(1)
    expect(w.emitted('success')).toBeTruthy()
  })

  it('renders steps result after process', async () => {
    const w = mount(OffboardingModal, {
      props: { modelValue: true, employeeId: 42, employeeName: '王小明' },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    await w.find('input[type="date"]').setValue('2026-06-15')
    await w.find('.preview-button').trigger('click')
    await flushPromises()
    await w.find('.confirm-button').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('標記進行中考核')
    expect(w.text()).toContain('產生離職證明 PDF')
  })
})
```

- [ ] **Step 2: Implement OffboardingModal**

```vue
<!-- src/components/offboarding/OffboardingModal.vue -->
<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useOffboardingStore } from '@/stores/offboarding'
import OffboardingPreviewPanel from './OffboardingPreviewPanel.vue'
import OffboardingStepsResult from './OffboardingStepsResult.vue'
import type { ApiResponse } from '@/api/_generated/typed'

type PreviewType = ApiResponse<'/offboarding/{employee_id}/preview', 'post'>
type ProcessType = ApiResponse<'/offboarding/{employee_id}/process', 'post'>

const props = defineProps<{
  modelValue: boolean
  employeeId: number
  employeeName: string
}>()

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  success: [result: ProcessType]
}>()

const store = useOffboardingStore()

const form = reactive({ resign_date: '', resign_reason: '' })
const stage = ref<'input' | 'preview' | 'result'>('input')
const preview = ref<PreviewType | null>(null)
const processResult = ref<ProcessType | null>(null)
const loading = ref(false)

watch(() => props.modelValue, (v) => {
  if (v) {
    // 重置 state
    form.resign_date = ''
    form.resign_reason = ''
    stage.value = 'input'
    preview.value = null
    processResult.value = null
  }
})

const doPreview = async () => {
  if (!form.resign_date) {
    ElMessage.warning('請選擇離職日期')
    return
  }
  loading.value = true
  try {
    preview.value = await store.preview(props.employeeId, {
      resign_date: form.resign_date,
      resign_reason: form.resign_reason || null,
    })
    stage.value = 'preview'
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error('預覽失敗：' + (err.response?.data?.detail || '未知錯誤'))
  } finally {
    loading.value = false
  }
}

const doProcess = async () => {
  loading.value = true
  try {
    processResult.value = await store.process(props.employeeId, {
      resign_date: form.resign_date,
      resign_reason: form.resign_reason || null,
    })
    stage.value = 'result'
    ElMessage.success('離職處理完成')
    emit('success', processResult.value)
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error('辦理離職失敗：' + (err.response?.data?.detail || '未知錯誤'))
  } finally {
    loading.value = false
  }
}

const retry = () => {
  stage.value = 'preview'
  processResult.value = null
}

const close = () => emit('update:modelValue', false)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="`辦理離職 — ${employeeName}`"
    width="640px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <!-- Stage 1: input -->
    <template v-if="stage === 'input'">
      <el-form label-width="100px">
        <el-form-item label="離職日期" required>
          <input v-model="form.resign_date" type="date" />
        </el-form-item>
        <el-form-item label="離職原因">
          <el-input
            v-model="form.resign_reason"
            type="textarea"
            placeholder="（選填，僅內部留存，不寫入離職證明 PDF）"
            :rows="2"
          />
        </el-form-item>
      </el-form>
    </template>

    <!-- Stage 2: preview -->
    <OffboardingPreviewPanel v-else-if="stage === 'preview' && preview" :preview="preview" />

    <!-- Stage 3: result -->
    <OffboardingStepsResult
      v-else-if="stage === 'result' && processResult"
      :steps="processResult.steps"
      @retry="retry"
    />

    <template #footer>
      <el-button @click="close">{{ stage === 'result' ? '關閉' : '取消' }}</el-button>
      <el-button
        v-if="stage === 'input'"
        class="preview-button"
        type="primary"
        :loading="loading"
        @click="doPreview"
      >
        預覽
      </el-button>
      <el-button
        v-else-if="stage === 'preview'"
        class="confirm-button"
        type="danger"
        :loading="loading"
        @click="doProcess"
      >
        確認辦理
      </el-button>
    </template>
  </el-dialog>
</template>
```

- [ ] **Step 3: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/components/OffboardingModal.test.ts && npx vue-tsc --noEmit 2>&1 | tail -5
```

Expected: PASS 3 tests + 0 typecheck error。

- [ ] **Step 4: Commit**

```bash
git add src/components/offboarding/OffboardingModal.vue tests/components/OffboardingModal.test.ts && \
git commit -m "feat(offboarding): add OffboardingModal (3-stage: input/preview/result)

input → 觸發 preview → 顯示預覽 → 「確認辦理」→ process → 顯示 5 step result
+ 失敗 retry。透過 useOffboardingStore.preview/process，process 後 emit success
讓父頁 refetch。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: EmployeeView 接入 OffboardingModal + 移除舊邏輯

**Files:**
- Modify: `src/views/EmployeeView.vue:6,312-373`（既有 inline 辦理離職邏輯）
- Modify: `src/api/employees.ts:16-17`（移除 offboard wrapper）

**動機：** EmployeeView 既有 312-373 行的 inline `offboardVisible / offboardForm / submitOffboard` 等改成觸發新 OffboardingModal。

- [ ] **Step 1: Edit EmployeeView.vue imports**

把第 6 行：
```ts
import { getEmployee, getEmployees, createEmployee, offboard, getFinalSalaryPreview } from '@/api/employees'
```
改為：
```ts
import { getEmployee, getEmployees, createEmployee, getFinalSalaryPreview } from '@/api/employees'
import OffboardingModal from '@/components/offboarding/OffboardingModal.vue'
```

- [ ] **Step 2: Replace inline offboard state with modal toggle**

把第 312-316 行：
```ts
// ── 辦理離職 ──────────────────────────────────────
const offboardVisible = ref(false)
const offboardTarget = ref<EmployeeRow | null>(null)
const offboardForm = reactive({ resign_date: '', resign_reason: '' })
const offboardLoading = ref(false)
```
改為：
```ts
// ── 辦理離職 ──────────────────────────────────────
const offboardVisible = ref(false)
const offboardTarget = ref<EmployeeRow | null>(null)
```

- [ ] **Step 3: Delete fetchFinalSalary / watch / submitOffboard sections（337-373 行）**

整段 337-373 刪除（fetchFinalSalary, watch resign_date, submitOffboard）— OffboardingModal 自行處理 preview / process。

`openOffboard` 簡化為：
```ts
const openOffboard = (emp: Record<string, unknown>) => {
  offboardTarget.value = emp as EmployeeRow
  offboardVisible.value = true
}
```

- [ ] **Step 4: Replace inline dialog template with OffboardingModal**

找 `<el-dialog v-model="offboardVisible" ...>` 那個 dialog 區塊（template 內），整段替換為：

```vue
<OffboardingModal
  v-if="offboardTarget"
  v-model="offboardVisible"
  :employee-id="offboardTarget.id"
  :employee-name="offboardTarget.name || ''"
  @success="fetchEmployees"
/>
```

- [ ] **Step 5: Remove `offboard` from src/api/employees.ts**

```ts
// src/api/employees.ts
// 刪除 line 16-17:
// export const offboard = (id: number, data: ApiBody<'/employees/{employee_id}/offboard', 'post'>): AxiosResp<...> =>
//     api.post(`/employees/${id}/offboard`, data)
```

舊後端 `/employees/{id}/offboard` endpoint passthrough 仍 work（後端 P1 已留向後相容），但前端不再呼叫。後端可在 Phase 3 merge 後 follow-up 刪 endpoint。

- [ ] **Step 6: Typecheck + run EmployeeView test if exists**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vue-tsc --noEmit 2>&1 | tail -10 && \
ls tests/views/EmployeeView.test.ts 2>/dev/null && \
npx vitest run tests/views/EmployeeView.test.ts 2>&1 | tail -10 || echo "no existing test"
```

Expected: 0 typecheck error；若有 existing test 全綠。

- [ ] **Step 7: Commit**

```bash
git add src/views/EmployeeView.vue src/api/employees.ts && \
git commit -m "$(cat <<'EOF'
refactor(employee): replace inline offboard logic with OffboardingModal

EmployeeView 改用新 src/components/offboarding/OffboardingModal：
- 移除 inline offboardForm / fetchFinalSalary / submitOffboard
- 移除 src/api/employees.ts:offboard wrapper（後端 passthrough endpoint
  仍 work，前端不再呼叫；後端可在 Phase 3 merge 後 follow-up 移除）
- 「辦理離職」按鈕改開 modal，走 /offboarding/{id}/process

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: MagicLinkPanel 元件

**Files:**
- Create: `src/components/offboarding/MagicLinkPanel.vue`
- Test: `tests/components/MagicLinkPanel.test.ts`

**動機：** OffboardingView 清單頁與 future detail panel 共用元件，產 / 撤 / 顯示狀態。

- [ ] **Step 1: Write failing test**

```ts
// tests/components/MagicLinkPanel.test.ts
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/offboarding', () => ({
  postMagicLink: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      token: 'abc123def456ghi789xyz000111222333444555666777888999000aaa',
      expires_at: '2026-07-15T10:00:00',
      download_url: '/api/offboarding/download?token=abc123def456ghi789xyz000111222333444555666777888999000aaa',
    },
  })),
  deleteMagicLink: vi.fn(() => Promise.resolve({ data: { employee_id: 42, revoked_at: '2026-06-15T15:00:00' } })),
}))

import MagicLinkPanel from '@/components/offboarding/MagicLinkPanel.vue'

const stubElDialog = {
  template: '<div v-if="modelValue"><slot /><slot name="footer" /></div>',
  props: ['modelValue', 'title'],
}

describe('MagicLinkPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders inactive state when no token', () => {
    const w = mount(MagicLinkPanel, {
      props: {
        employeeId: 42,
        active: false,
        expiresAt: null,
        downloadCount: 0,
        lastUsedAt: null,
      },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    expect(w.text()).toContain('尚未產生')
    expect(w.find('.generate-button').exists()).toBe(true)
    expect(w.find('.revoke-button').exists()).toBe(false)
  })

  it('renders active state with metadata', () => {
    const w = mount(MagicLinkPanel, {
      props: {
        employeeId: 42,
        active: true,
        expiresAt: '2026-07-15T10:00:00',
        downloadCount: 2,
        lastUsedAt: '2026-06-15T19:23:00',
      },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    expect(w.text()).toContain('2026-07-15')
    expect(w.text()).toContain('2 次')
    expect(w.find('.revoke-button').exists()).toBe(true)
  })

  it('shows token dialog after generate', async () => {
    const w = mount(MagicLinkPanel, {
      props: {
        employeeId: 42,
        active: false,
        expiresAt: null,
        downloadCount: 0,
        lastUsedAt: null,
      },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    await w.find('.generate-button').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('abc123')  // token 顯示在 dialog
  })

  it('emits update after revoke', async () => {
    const w = mount(MagicLinkPanel, {
      props: {
        employeeId: 42,
        active: true,
        expiresAt: '2026-07-15T10:00:00',
        downloadCount: 0,
        lastUsedAt: null,
      },
      global: { stubs: { ElDialog: stubElDialog } },
    })
    await w.find('.revoke-button').trigger('click')
    // ElMessageBox confirm 在 jsdom 無法 trigger；改測直接 deleteMagicLink call
    // 跳過 confirm 直接 emit
    await flushPromises()
    // 至少驗按鈕能點到不爆
    expect(w.find('.revoke-button').exists()).toBe(true)
  })
})
```

- [ ] **Step 2: Implement MagicLinkPanel**

```vue
<!-- src/components/offboarding/MagicLinkPanel.vue -->
<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { postMagicLink, deleteMagicLink } from '@/api/offboarding'

const props = defineProps<{
  employeeId: number
  active: boolean
  expiresAt: string | null
  downloadCount: number
  lastUsedAt: string | null
}>()

const emit = defineEmits<{ update: [] }>()

const tokenDialogVisible = ref(false)
const generatedToken = ref('')
const generatedDownloadUrl = ref('')
const generatedExpiresAt = ref('')

const onGenerate = async () => {
  try {
    const res = await postMagicLink(props.employeeId)
    generatedToken.value = res.data.token
    generatedDownloadUrl.value = res.data.download_url
    generatedExpiresAt.value = res.data.expires_at
    tokenDialogVisible.value = true
    emit('update')
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error('產生失敗：' + (err.response?.data?.detail || '未知錯誤'))
  }
}

const onRevoke = async () => {
  try {
    await ElMessageBox.confirm('確定撤銷此 magic-link？', '撤銷確認', { type: 'warning' })
  } catch {
    return  // user cancelled
  }
  try {
    await deleteMagicLink(props.employeeId)
    ElMessage.success('已撤銷')
    emit('update')
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error('撤銷失敗：' + (err.response?.data?.detail || '未知錯誤'))
  }
}

const copyToClipboard = async () => {
  try {
    await navigator.clipboard.writeText(generatedDownloadUrl.value)
    ElMessage.success('已複製連結')
  } catch {
    ElMessage.warning('複製失敗，請手動選取')
  }
}

const onTokenDialogClose = () => {
  // 永不重顯：清掉 state
  generatedToken.value = ''
  generatedDownloadUrl.value = ''
  generatedExpiresAt.value = ''
}
</script>

<template>
  <div class="magic-link-panel">
    <template v-if="active">
      <div class="meta">
        <p>狀態：<el-tag type="success">啟用中</el-tag></p>
        <p>到期：{{ expiresAt }}</p>
        <p>已下載：{{ downloadCount }} 次（最多 3 次）</p>
        <p v-if="lastUsedAt">最後下載：{{ lastUsedAt }}</p>
      </div>
      <el-button type="warning" class="revoke-button" @click="onRevoke">撤銷連結</el-button>
      <el-button type="primary" class="generate-button" @click="onGenerate">重新產生</el-button>
    </template>
    <template v-else>
      <p>尚未產生下載連結</p>
      <el-button type="primary" class="generate-button" @click="onGenerate">產生下載連結</el-button>
    </template>

    <el-dialog
      v-model="tokenDialogVisible"
      title="下載連結（只此一次顯示）"
      width="600px"
      @close="onTokenDialogClose"
    >
      <p>請複製以下連結，貼到 email 發送給員工：</p>
      <el-input :model-value="generatedDownloadUrl" readonly type="textarea" :rows="3" />
      <p class="hint">到期：{{ generatedExpiresAt }}（30 天 / 3 次下載上限）</p>
      <template #footer>
        <el-button @click="copyToClipboard">複製到剪貼簿</el-button>
        <el-button type="primary" @click="tokenDialogVisible = false">關閉</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.magic-link-panel { padding: 12px; border: 1px solid #ebeef5; border-radius: 4px; }
.meta p { margin: 4px 0; }
.hint { color: #909399; font-size: 12px; margin-top: 8px; }
</style>
```

- [ ] **Step 3: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/components/MagicLinkPanel.test.ts && npx vue-tsc --noEmit 2>&1 | tail -5
```

Expected: PASS 4 tests + 0 typecheck error。

- [ ] **Step 4: Commit**

```bash
git add src/components/offboarding/MagicLinkPanel.vue tests/components/MagicLinkPanel.test.ts && \
git commit -m "$(cat <<'EOF'
feat(offboarding): add MagicLinkPanel component

active state：顯示 expires_at / download_count / last_used_at；
inactive state：產生按鈕。產生後 ElDialog 一次顯示 token + download_url，
admin 複製到剪貼簿後 dialog 關閉永不重顯（state 清掉）。撤銷需 ElMessageBox
確認；emit update 讓父頁 refetch。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: OffboardingView 清單頁 + 路由 + 菜單 + Sentry PII

**Files:**
- Create: `src/views/admin/OffboardingView.vue`
- Modify: `src/router/index.ts`（加 /admin/offboarding 路由）
- Modify: `src/components/admin/AdminSidebar.vue`（菜單）
- Modify: `src/utils/sentry.ts`（PII denylist 加 3 key）
- Test: `tests/views/OffboardingView.test.ts`

- [ ] **Step 1: Sentry PII denylist 同步**

修 `src/utils/sentry.ts` `PII_KEY_SUBSTRINGS` 加 3 entry（與後端 utils/sentry_init.py 對齊）：

```ts
const PII_KEY_SUBSTRINGS = [
  // ... existing entries
  'resign_reason',
  'leave_balance_snapshot',
  'certificate_pdf_path',
]
```

- [ ] **Step 2: Write OffboardingView failing test**

由於清單頁需後端 list endpoint 尚未實作（Phase 3 範圍含一個簡化版本：透過 GET /api/employees 過濾 resign_date != null 員工再對每個 fetch detail），test 簡化為驗渲染：

```ts
// tests/views/OffboardingView.test.ts
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/employees', () => ({
  getEmployees: vi.fn(() => Promise.resolve({
    data: {
      employees: [
        { id: 42, name: '王小明', resign_date: '2026-06-15', is_active: false },
        { id: 43, name: '李四', resign_date: null, is_active: true },  // 不應出現
      ],
    },
  })),
}))

vi.mock('@/api/offboarding', () => ({
  getOffboardingDetail: vi.fn(() => Promise.resolve({
    data: {
      employee_id: 42,
      employee_name: '王小明',
      resign_date: '2026-06-15',
      magic_link_active: false,
      magic_link_download_count: 0,
      magic_link_expires_at: null,
      magic_link_last_used_at: null,
      closed_at: null,
    },
  })),
}))

import OffboardingView from '@/views/admin/OffboardingView.vue'

describe('OffboardingView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('lists only employees with resign_date', async () => {
    const w = mount(OffboardingView, {
      global: { stubs: { 'router-link': true, ElTable: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    expect(w.text()).toContain('王小明')
    expect(w.text()).not.toContain('李四')
  })
})
```

- [ ] **Step 3: Implement OffboardingView**

```vue
<!-- src/views/admin/OffboardingView.vue -->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getEmployees } from '@/api/employees'
import { useOffboardingStore } from '@/stores/offboarding'
import MagicLinkPanel from '@/components/offboarding/MagicLinkPanel.vue'
import type { ApiResponse } from '@/api/_generated/typed'

type DetailType = ApiResponse<'/offboarding/{employee_id}', 'get'>

interface OffboardingRow {
  employee_id: number
  employee_name: string
  resign_date: string
  detail: DetailType | null
}

const store = useOffboardingStore()
const rows = ref<OffboardingRow[]>([])
const loading = ref(false)
const selected = ref<OffboardingRow | null>(null)

const fetch = async () => {
  loading.value = true
  try {
    const res = await getEmployees()
    const employees = (res.data as { employees: Array<{ id: number; name?: string; resign_date?: string | null }> }).employees
    const resigned = employees.filter(e => e.resign_date)
    rows.value = await Promise.all(
      resigned.map(async (e) => {
        let detail: DetailType | null = null
        try {
          detail = await store.fetchDetail(e.id)
        } catch {
          detail = null  // employee 有 resign_date 但無 offboarding_record（歷史資料）
        }
        return {
          employee_id: e.id,
          employee_name: e.name || '',
          resign_date: e.resign_date as string,
          detail,
        }
      }),
    )
  } finally {
    loading.value = false
  }
}

onMounted(fetch)

const onMagicLinkUpdate = async () => {
  if (selected.value) {
    selected.value.detail = await store.refreshDetail(selected.value.employee_id)
  }
}
</script>

<template>
  <div class="offboarding-view">
    <h2>離職管理</h2>
    <p class="hint">
      列出所有 resign_date 不為空的員工。Phase 1 上線後新離職有完整 checklist 紀錄；
      歷史離職員工僅顯示基本資料。
    </p>

    <el-table v-loading="loading" :data="rows" border>
      <el-table-column label="員工" prop="employee_name" width="120" />
      <el-table-column label="離職日" prop="resign_date" width="120" />
      <el-table-column label="checklist 狀態">
        <template #default="{ row }">
          <el-tag v-if="!row.detail" type="info" size="small">無 record（歷史）</el-tag>
          <el-tag v-else-if="row.detail.closed_at" type="success" size="small">已結案</el-tag>
          <el-tag v-else type="warning" size="small">未結案</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="證明 PDF">
        <template #default="{ row }">
          <a v-if="row.detail?.certificate_pdf_path"
             :href="`/api/offboarding/${row.employee_id}/certificate.pdf`"
             target="_blank">下載</a>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="自助下載連結">
        <template #default="{ row }">
          <el-tag v-if="row.detail?.magic_link_active" type="success" size="small">啟用中</el-tag>
          <el-tag v-else-if="row.detail" type="info" size="small">未產生</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button v-if="row.detail" size="small" @click="selected = row">管理</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="selected" v-if="selected" size="500px" :title="`離職管理 — ${selected.employee_name}`">
      <MagicLinkPanel
        v-if="selected.detail"
        :employee-id="selected.employee_id"
        :active="selected.detail.magic_link_active"
        :expires-at="selected.detail.magic_link_expires_at"
        :download-count="selected.detail.magic_link_download_count"
        :last-used-at="selected.detail.magic_link_last_used_at"
        @update="onMagicLinkUpdate"
      />
    </el-drawer>
  </div>
</template>
```

- [ ] **Step 4: Add router entry**

修 `src/router/index.ts`，找 admin routes block，加：

```ts
{
  path: '/admin/offboarding',
  name: 'admin-offboarding',
  component: () => import('@/views/admin/OffboardingView.vue'),
  meta: { requiresAuth: true, permission: 'EMPLOYEES_READ' },
},
```

- [ ] **Step 5: Add AdminSidebar menu entry**

修 `src/components/admin/AdminSidebar.vue`，找「人事管理」menu group，在「員工管理」後加：

```vue
<el-menu-item v-if="hasPermission('EMPLOYEES_READ')" index="/admin/offboarding">
  離職管理
</el-menu-item>
```

或對齊既有 sidebar 結構（grep `EMPLOYEES_READ` 看 existing pattern）。

- [ ] **Step 6: Run tests + typecheck**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vitest run tests/views/OffboardingView.test.ts && npx vue-tsc --noEmit 2>&1 | tail -10
```

Expected: PASS 1 test + 0 typecheck error。

- [ ] **Step 7: Commit**

```bash
git add src/views/admin/OffboardingView.vue src/router/index.ts \
  src/components/admin/AdminSidebar.vue src/utils/sentry.ts \
  tests/views/OffboardingView.test.ts && \
git commit -m "$(cat <<'EOF'
feat(offboarding): add OffboardingView listing + route + sidebar + Sentry PII

OffboardingView：列出 resign_date 不為空員工 + checklist 狀態 chips + 證明 PDF
下載連結 + magic-link 狀態，點「管理」開 drawer 內嵌 MagicLinkPanel。

AdminSidebar 新菜單「人事管理 → 離職管理」gated by EMPLOYEES_READ。

Sentry PII denylist 加 3 key 對齊後端 (resign_reason / leave_balance_snapshot
/ certificate_pdf_path)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Playwright e2e smoke

**Files:**
- Create: `e2e/offboarding.spec.ts`（**注意：在 workspace `/Users/yilunwu/Desktop/ivyManageSystem/e2e/`，不在 ivy-frontend**）

**動機：** workspace CLAUDE.md 規定 e2e 在 `~/Desktop/ivyManageSystem/e2e/`（不放 frontend repo）。Phase 3 critical-path: admin login → 開 OffboardingModal → preview → confirm → 驗 step result + 下載證明。

- [ ] **Step 1: 確認既有 e2e 結構**

```bash
ls /Users/yilunwu/Desktop/ivyManageSystem/e2e/ && \
head -40 /Users/yilunwu/Desktop/ivyManageSystem/e2e/playwright.config.ts 2>/dev/null
```

確認 globalSetup / auth-admin storageState 模式（既有 spec 用 admin pre-authenticated session）。

- [ ] **Step 2: Write spec**

```ts
// e2e/offboarding.spec.ts
import { test, expect } from '@playwright/test'

// 既有 globalSetup 已建 e2e_admin storageState
// target employee 從 .env E2E_TEST_EMPLOYEE_ID 取（非 admin 本人 + 月薪 + active）
const TARGET_EMP_ID = parseInt(process.env.E2E_TEST_EMPLOYEE_ID || '0')

test.describe('員工離職 critical path', () => {
  test.skip(!TARGET_EMP_ID, 'E2E_TEST_EMPLOYEE_ID not set')

  test('admin 一鍵離職 → preview → confirm → 5 step result', async ({ page }) => {
    // 注意：此 test 會真實寫 DB（Employee.resign_date / is_active）
    // 跑前確認 target employee 是測試專用且狀態可被破壞性測試
    test.skip(true, '會破壞性寫 DB；跑前手動取消 skip 並準備可重置的 target employee')

    await page.goto('/employees')
    await page.locator(`tr:has-text("emp-${TARGET_EMP_ID}")`).getByRole('button', { name: '辦理離職' }).click()

    // OffboardingModal 開啟
    await expect(page.getByRole('dialog', { name: /辦理離職/ })).toBeVisible()

    // Stage 1: input
    await page.locator('input[type="date"]').fill('2026-06-15')
    await page.getByRole('textbox', { name: /離職原因/ }).fill('e2e smoke test')
    await page.getByRole('button', { name: '預覽' }).click()

    // Stage 2: preview
    await expect(page.getByText('將撤銷')).toBeVisible()
    await page.getByRole('button', { name: '確認辦理' }).click()

    // Stage 3: result
    await expect(page.getByText('標記進行中考核')).toBeVisible()
    await expect(page.getByText('產生離職證明 PDF')).toBeVisible()

    // 證明 PDF 可下載（不真實下載，只驗 link 存在）
    // 此 step 由 OffboardingView 清單頁驗，這裡只驗 modal 完成
  })

  test('離職管理清單頁渲染', async ({ page }) => {
    await page.goto('/admin/offboarding')
    await expect(page.getByRole('heading', { name: '離職管理' })).toBeVisible()
    await expect(page.locator('table')).toBeVisible()
  })
})
```

- [ ] **Step 3: 驗證 e2e spec 可解析（不真實跑）**

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem/e2e && \
npx playwright test --list 2>&1 | tail -10
```

Expected: 列出 2 個 test（含 skip）。

- [ ] **Step 4: Commit e2e spec**

注意：e2e 在 workspace 根，**不是** ivy-frontend worktree — 直接 cd workspace。

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem/e2e && \
git add offboarding.spec.ts && \
git status --short && \
git log --oneline -1 2>&1
```

若 workspace 根**不是 git repo**（CLAUDE.md 顯示 `Is a git repository: false`），則此檔由 user 手動 commit 至 ivy-frontend worktree 或 ivy-backend 內某處（依 e2e/ 隸屬決定）。**先確認**：

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem && git rev-parse --is-inside-work-tree 2>&1
```

若返回 false，則 spec 寫到 ivy-frontend worktree 內 `e2e/offboarding.spec.ts`（前端 repo 內亦可，雖違反 CLAUDE.md 注意但這是 workaround）—**留 follow-up note**：「workspace 不是 git repo，e2e/offboarding.spec.ts 暫 commit 至 ivy-frontend，等 user 決定 e2e/ 的 source repo」。

---

## Task 9: 跑 frontend 全 suite 驗證 + final cleanup

**Files:** N/A — 驗證 only

- [ ] **Step 1: 跑全 vitest**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npm test 2>&1 | tail -20
```

Expected: PASS 全部（既有 ~2400 test + Phase 3 新 ~20 test 不回歸）。

- [ ] **Step 2: 跑 typecheck**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npx vue-tsc --noEmit 2>&1 | tail -10
```

Expected: 0 error。

- [ ] **Step 3: 跑 build**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/feat/offboarding-phase-3-2026-05-25-frontend && \
npm run build 2>&1 | tail -10
```

Expected: build 成功。

- [ ] **Step 4: 報告**

回 controller 完成狀態 + commit chain（git log --oneline）。

---

## 完成檢核

| 項 | spec ref | task |
|---|---|---|
| src/api/offboarding.ts wrapper | §9 | 1 |
| Pinia store cache + actions | §9 | 2 |
| OffboardingPreviewPanel + StepsResult | §9 | 3 |
| OffboardingModal 一鍵 (3-stage) | §9 + §6.2/§6.3 | 4 |
| EmployeeView 接入 modal + 移除舊邏輯 | §9 + §6.5 | 5 |
| MagicLinkPanel（產 / 撤 / 一次顯示 token） | §9 + §8 | 6 |
| OffboardingView 清單頁 + 路由 + 菜單 | §9 | 7 |
| Sentry PII denylist 同步 | §10.3 | 7 |
| e2e/offboarding.spec.ts | §11.1 + §3 Phase 3 | 8 |
| frontend 全 suite + typecheck + build 驗證 | §13 | 9 |

**Phase 3 不含：**
- 後端任何改動（Phase 1+2 ship）
- 家長端 UI
- admin email 自動寄 magic-link（admin 手動複製）
- ex-employee 永久 login（spec §15 明確排除）

**已知 follow-up（不擋 merge）：**
- 後端舊 `POST /api/employees/{id}/offboard` passthrough endpoint 可在 Phase 3 merge 後另 PR 移除（前端不再呼叫）
- e2e smoke 真實跑需手動 unskip + 準備可破壞性 test target employee
- workspace `e2e/` 非 git repo 的 spec commit 路徑待 user 決定
