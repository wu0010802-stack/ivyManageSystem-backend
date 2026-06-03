# PDPA Phase 2 — 前端（P2-4 家長 + P2-5 admin）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 或 superpowers:executing-plans。前端為 TS-only（`<script setup lang="ts">`，禁 `any`）。Steps 用 `- [ ]`。

**Goal:** 讓家長能在被 consent gate 攔下時重簽當期政策（re-consent modal）、即時撤回 granular scope；讓 admin 能決議 DSR queue（approve/reject）與管理政策版本（觸發重簽）。

**Architecture:** P2-4 在既有家長 LIFF axios interceptor 攔 `403 + X-Consent-Required` → 彈既有 `ConsentModal` 簽當期 policy；MeView 個資權利區（`PrivacyRightsView`）已存在，只補 opt-out 即時反饋。P2-5 仿 `SettingsUsersTab` 寫 `DsrRequestsView` + `PolicyVersionsView`，掛 `/settings` tab，`DSR_MANAGE` gate。

**Tech Stack:** Vue 3、TypeScript strict、Element Plus、axios wrapper（`src/api/index.ts` / `src/parent/api/index.ts`）、Vitest（teleport stub 見 [[feedback_sheet_teleport_test_pattern]]）。

---

## 前置依賴與排序

1. **依賴後端 plan（`2026-06-02-pdpa-phase2-backend-enforcement.md`）的契約先落地**：403 `X-Consent-Required` header（Task 7）、`GET/POST /api/admin/dsr-requests`（Task 11）、`DSR_MANAGE`（Task 10）、policy 端點。
2. **codegen 順序（CLAUDE.md OpenAPI 防漂移）**：後端 PR 合併後，先
   ```bash
   cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
   cd ~/Desktop/ivy-frontend && npm run gen:api
   ```
   再寫消費 `schema.d.ts` 型別的 admin api wrapper（Task 3）。**Task 3 起的型別以 `_generated/typed` 為準，不手編 interface。**
3. **PR 邊界**：P2-4（Task 1–2）/ P2-5（Task 3–6）各自獨立 commit。

---

## 現況（explore 已確認，避免重造）

- `src/parent/api/index.ts:96-209` — 家長 axios interceptor（已處理 401 refresh + error normalization；**未讀 response header**，403 header 處理為新增點）。
- `src/parent/views/MeView.vue:92-97` — PREFS 已含 `privacy_rights` 入口 → `/me/privacy-rights`。
- `src/parent/views/PrivacyRightsView.vue` — **已完整實作**（同意紀錄 / 刪除 / 更正 / 停止處理）。
- `src/parent/api/consent.ts` — 已有 7 個 parent wrapper（getCurrentPolicy / getMyConsents / writeConsent / submitDelete / submitCorrect / submitOptOut / listMyDsrRequests）。
- `src/parent/components/ConsentModal.vue` — 已存在（scope checkbox）。
- `src/components/settings/SettingsUsersTab.vue` — admin CRUD 樣板（list + dialog + approve/reject pattern）。
- `src/views/SettingsView.vue` — tabs 容器；`src/router/index.ts:200` — `/settings` 路由。
- `src/utils/auth.ts:189` — `hasPermission(name)`；`src/constants/permissions.ts` — `PERMISSION_NAMES`。

---

## P2-4：家長端

### Task 1: interceptor 攔 403 `X-Consent-Required` → re-consent modal

**Files:**
- Modify: `src/parent/api/index.ts`（interceptor，~line 160-186）
- Create: `src/parent/composables/useConsentGate.ts`（singleton event：觸發 re-consent）
- Modify: 家長 app 根組件（如 `src/parent/App.vue` 或 LIFF layout）掛 modal
- Test: `src/parent/composables/__tests__/useConsentGate.spec.ts`

設計：interceptor 偵測 `error.response?.status === 403 && headers['x-consent-required']` → 呼叫 `useConsentGate().require(scope)`（推一個 reactive flag）→ 根組件 watch flag 彈 `ConsentModal`（簽當期 policy）→ 簽完 `writeConsent` → 關閉 + 可選 retry 原請求。

- [ ] **Step 1: 寫 failing test（composable 純邏輯）**

```typescript
// useConsentGate.spec.ts
import { describe, it, expect } from 'vitest'
import { useConsentGate } from '../useConsentGate'

describe('useConsentGate', () => {
  it('require(scope) 設 pending scope 與 visible', () => {
    const g = useConsentGate()
    g.reset()
    expect(g.visible.value).toBe(false)
    g.require('service_essential')
    expect(g.visible.value).toBe(true)
    expect(g.pendingScope.value).toBe('service_essential')
  })
  it('resolve() 清除狀態', () => {
    const g = useConsentGate()
    g.require('service_essential'); g.resolve()
    expect(g.visible.value).toBe(false)
  })
})
```

- [ ] **Step 2: Run，確認 fail**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/parent/composables/__tests__/useConsentGate.spec.ts`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 composable（module-singleton）**

```typescript
// src/parent/composables/useConsentGate.ts
import { ref } from 'vue'

const visible = ref(false)
const pendingScope = ref<string | null>(null)

export function useConsentGate() {
  return {
    visible,
    pendingScope,
    require(scope: string) { pendingScope.value = scope; visible.value = true },
    resolve() { visible.value = false; pendingScope.value = null },
    reset() { visible.value = false; pendingScope.value = null },
  }
}
```

- [ ] **Step 4: Run，確認 pass**

- [ ] **Step 5: interceptor 接上（在 ~line 160-186，kill-switch 後、error normalization 前）**

```typescript
import { useConsentGate } from '@/parent/composables/useConsentGate'
// ...在 error handler：
const consentScope = error.response?.headers?.['x-consent-required']
if (error.response?.status === 403 && consentScope) {
  useConsentGate().require(String(consentScope))
  // 不在此 retry；簽署完成由 modal 流程處理，原請求讓 caller 自然失敗一次
}
```

根組件（家長 app 入口）掛：
```vue
<ConsentModal v-if="gate.visible.value" :scope="gate.pendingScope.value"
  @signed="onSigned" @cancel="gate.resolve()" />
```
`onSigned` 內 `await writeConsent({ scope, policy_version_id, consented: true })`（policy_version_id 取自 `getCurrentPolicy()`）→ `gate.resolve()`。

> **執行注意**：先讀 `src/parent/components/ConsentModal.vue` 現有 props/emit（`grep "defineProps\|defineEmits" ConsentModal.vue`）；若它已自帶簽署邏輯，`onSigned` 改為直接 `gate.resolve()`。家長 app 根組件路徑：`grep -rn "createApp\|liff" src/parent/main.ts`。

- [ ] **Step 6: Commit**

```bash
git add src/parent/composables/useConsentGate.ts src/parent/api/index.ts \
        src/parent/composables/__tests__/useConsentGate.spec.ts <root component>
git commit -m "feat(parent): 攔 403 X-Consent-Required 彈 re-consent modal 簽當期政策"
```

---

### Task 2: opt-out 即時反饋（PrivacyRightsView 撤回 granular scope）

**Files:**
- Modify: `src/parent/views/PrivacyRightsView.vue`（撤回 scope 的成功反饋）
- Test: 既有 PrivacyRightsView 測試（若有）擴充

後端 Task 9 把 opt-out 改即時（granular scope 立即生效）。前端確認「撤回」按鈕呼叫 `submitOptOut({ scope })` 後，UI 顯示「已即時停止」而非「申請已送出待審」，並 refetch `getMyConsents()` 反映最新狀態。

- [ ] **Step 1**: 讀 `PrivacyRightsView.vue` 現有 opt-out 區段（`grep "submitOptOut\|opt.out\|停止處理" src/parent/views/PrivacyRightsView.vue`）。
- [ ] **Step 2**: 寫/擴充 test：點撤回 → 呼叫 submitOptOut → 成功訊息為「即時生效」+ refetch consents。
- [ ] **Step 3**: 調整文案 + `await getMyConsents()` refetch；`service_essential` 撤回按鈕隱藏/禁用（後端回 4xx，前端不該提供）。
- [ ] **Step 4**: Run + Commit `feat(parent): opt-out granular scope 即時生效反饋 + service_essential 不顯示撤回`

---

## P2-5：admin 端

### Task 3: admin DSR / policy api wrapper（codegen 後）

**Files:**
- Modify: `src/api/consent.ts`（或新建 `src/api/dsr.ts`）
- 前置：後端合併 + `npm run gen:api` 完成

- [ ] **Step 1**: 確認 `schema.d.ts` 含 `/admin/dsr-requests` 等 path（`grep "admin/dsr-requests\|admin/policies" src/api/_generated/schema.d.ts`）。無 → 後端 response_model 沒寫好，回後端補（CLAUDE.md：缺 response_model 前端拿 unknown）。
- [ ] **Step 2**: 寫 wrapper（仿 `src/api/employees.ts`，型別走 `_generated/typed`）：

```typescript
import api from './index'
import type { ApiQuery, ApiBody, AxiosResp } from './_generated/typed'

export const listDsrRequests = (params?: ApiQuery<'/admin/dsr-requests', 'get'>): AxiosResp<'/admin/dsr-requests', 'get'> =>
  api.get('/admin/dsr-requests', { params })
export const approveDsrRequest = (id: number, data: ApiBody<'/admin/dsr-requests/{req_id}/approve', 'post'>): AxiosResp<'/admin/dsr-requests/{req_id}/approve', 'post'> =>
  api.post(`/admin/dsr-requests/${id}/approve`, data)
export const rejectDsrRequest = (id: number, data: ApiBody<'/admin/dsr-requests/{req_id}/reject', 'post'>): AxiosResp<'/admin/dsr-requests/{req_id}/reject', 'post'> =>
  api.post(`/admin/dsr-requests/${id}/reject`, data)
// policy 管理端點（後端若提供 admin policy CRUD；否則對齊後端實際路徑）
```

- [ ] **Step 3**: Commit `feat(api): admin DSR queue / policy wrapper（消費 codegen 型別）`

> **路徑佔位以後端實際 OpenAPI key 為準**（後端 Task 11 端點定案後，path literal 對齊 `schema.d.ts`）。

---

### Task 4: `DsrRequestsView`（queue approve/reject）

**Files:**
- Create: `src/views/DsrRequestsView.vue`
- Test: `src/views/__tests__/DsrRequestsView.spec.ts`（mount + teleport stub）

- [ ] **Step 1**: 寫 failing test（mount → 呼叫 listDsrRequests → 渲染 pending 列；點 reject → 開 dialog 填 decision_note → 呼叫 rejectDsrRequest）。`global: { stubs: { teleport: true } }`。
- [ ] **Step 2**: fail
- [ ] **Step 3**: 實作（仿 SettingsUsersTab：`el-table` 列 DSR + `el-dialog` 填 decision_note + approve/reject 呼叫 api + refetch）。request_type/status 用中文 label map。
- [ ] **Step 4**: pass + Commit `feat(admin): DsrRequestsView 個資權利請求佇列（approve/reject + decision_note）`

---

### Task 5: `PolicyVersionsView`（上傳新版觸發重簽）

**Files:**
- Create: `src/views/PolicyVersionsView.vue`
- Test: `src/views/__tests__/PolicyVersionsView.spec.ts`

- [ ] list 既有 policy 版本 + 上傳新版（version / effective_at / 文件 / summary）→ 呼叫後端 policy 端點。新版生效後既有家長下次進 portal 被 Task 1 攔重簽。
- [ ] test + 實作 + Commit `feat(admin): PolicyVersionsView 政策版本管理（升版觸發重簽）`

> 後端 policy 管理端點若 Phase 2 未含 admin CRUD（spec §3.3 只列 `GET /policies/current`），本 Task 需後端補 `POST /api/admin/policies` + publish——**回後端 plan 加一個 task 或標 follow-up**。執行前確認後端是否提供。

---

### Task 6: 路由 + tab + `DSR_MANAGE` gate + 權限字串同步

**Files:**
- Modify: `src/constants/permissions.ts`（加 `DSR_MANAGE`）
- Modify: `src/views/SettingsView.vue`（加 2 個 tab）或 `src/router/index.ts`（新路由 + meta 權限）
- Test: 權限 gate 測試

- [ ] **Step 1**: `src/constants/permissions.ts` `PERMISSION_NAMES` 加 `DSR_MANAGE: 'DSR_MANAGE'`（與後端 Task 10 同步，CLAUDE.md 陷阱 #1）。
- [ ] **Step 2**: SettingsView 加 tab（`v-if="hasPermission('DSR_MANAGE')"`）掛 DsrRequestsView / PolicyVersionsView；或新路由 `/dsr-management` + router guard。
- [ ] **Step 3**: test：無 DSR_MANAGE → tab/路由不可見。
- [ ] **Step 4**: Commit `feat(admin): DSR/policy 管理掛 settings + DSR_MANAGE 權限 gate`

---

## Self-Review（plan 對 spec §3.4/§3.5 覆蓋）

- spec §3.4 家長 re-consent modal（攔 X-Consent-Required）→ Task 1 ✓
- spec §3.4 MeView 個資權利 + 撤回 granular → 既有 PrivacyRightsView + Task 2 ✓
- spec §3.4 admin DsrRequestsView → Task 4 ✓
- spec §3.4 admin PolicyVersionsView → Task 5 ✓
- spec §3.4 admin gated by DSR_MANAGE → Task 6 ✓
- 契約同步（codegen / 權限字串）→ 前置 + Task 3/6 ✓

**Flagged（執行前須確認）：**
1. `ConsentModal.vue` 是否已自帶簽署邏輯（影響 Task 1 onSigned 寫法）——執行前讀。
2. 後端是否提供 admin policy CRUD 端點（影響 Task 5）——spec §3.3 只列 `GET /policies/current`，可能需回後端補。
3. admin views 掛 `/settings` tab vs 獨立路由——UX 偏好，預設 tab（最小改動）。
