"""AUDIT-COVERAGE sweep：每個 mutation 端點都必須「被 AuditMiddleware 稽核」或
「明確列入 AUDIT_EXEMPT 白名單並附理由」。

Why（系統設計審查 2026-06-25，主題 A）：稽核覆蓋採「中央 ENTITY_PATTERNS opt-in +
_parse_entity_type 回 None 即靜默跳過」的 opt-out 反模式——新增寫 router 只要忘了補
pattern，該模組所有寫操作就**零 audit_logs 且零失敗訊號**上線（已重複踩過：才藝批次
點名、attachments、本次掃出的 shifts / 才藝鐘點費 / 給藥 / 員工懲處 等）。

本 sweep 把「漏配稽核」從靜默缺口變成 CI 擋線：枚舉 main.app 所有 mutation 端點
（POST/PUT/PATCH/DELETE），斷言每條 path 要嘛被 `utils.audit._parse_entity_type`
覆蓋（middleware 會落 audit_logs），要嘛在下方 AUDIT_EXEMPT 白名單（公開/認證/自助/
端點自審/高量點名/唯讀預覽/meta）並寫明理由。新增任何「未覆蓋且不在白名單」的
mutation 端點即 fail，迫使開發者「補 ENTITY_PATTERNS」或「有意識地豁免並寫理由」。

注意：本 sweep 只檢查「path 是否會被稽核」，不檢查稽核內容正確性（後者屬各端點測試）。
與 tests/test_mutation_guard_coverage.py（授權守衛覆蓋）為姊妹 sweep，同一 route-
enumeration 模板。
"""

import os
import sys

from fastapi.routing import APIRoute

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main
from utils.audit import _parse_entity_type

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# 刻意「不經 AuditMiddleware 稽核」的 mutation 端點（route.path → 理由）。
# 新增寫端點預設應被 ENTITY_PATTERNS 覆蓋；確需豁免者才加進此白名單並寫理由。
AUDIT_EXEMPT: dict[str, str] = {
    # ── 認證端點 / 登入者自助 session 管理（非業務資料異動）──
    "/api/auth/login": "登入（認證端點）",
    "/api/auth/logout": "登出（自助）",
    "/api/auth/refresh": "access token 刷新",
    "/api/auth/sessions/logout-all": "登出自己所有 session（自助）",
    "/api/auth/sessions/{family_id}": "撤銷自己某 session（自助）",
    # ── 公開端點（匿名可達：公開報名 / 查詢）──
    "/api/activity/public/inquiries": "公開才藝諮詢（匿名）",
    "/api/activity/public/query": "公開報名查詢（匿名）",
    "/api/activity/public/query-by-token": "公開報名查詢（query token）",
    "/api/activity/public/register": "公開才藝報名（匿名）",
    "/api/activity/public/registrations/{registration_id}/courses/{course_id}/confirm-promotion": "家長憑 token 確認候補轉正",
    "/api/activity/public/registrations/{registration_id}/courses/{course_id}/decline-promotion": "家長憑 token 婉拒候補",
    # ── webhook（無 JWT）──
    "/api/internal/uptime-webhook": "uptime 監控 webhook",
    "/api/line/webhook": "LINE webhook（簽章驗證）",
    # ── 端點自審：endpoint 內自行 write_explicit_audit / write_audit_in_session ──
    "/api/activity/attendance/sessions": "才藝點名 session 建立（attendance.py 自審）",
    "/api/activity/attendance/sessions/batch": "才藝點名批次（attendance.py 自審）",
    "/api/activity/attendance/sessions/{session_id}": "才藝點名 session 刪除（attendance.py 自審）",
    "/api/activity/attendance/sessions/{session_id}/records": "才藝點名 records（attendance.py 自審）",
    "/api/portal/activity/attendance/sessions/{session_id}/records": "教師端才藝點名 records（自審）",
    "/api/admin/dsr-requests/{req_id}/approve": "個資請求核准（dsr_admin.py 自審）",
    "/api/admin/dsr-requests/{req_id}/reject": "個資請求駁回（dsr_admin.py 自審）",
    "/api/admin/policies": "系統政策（policies_admin.py 自審）",
    "/api/roles": "角色建立（permissions_admin.py write_audit_in_session 自審）",
    "/api/roles/{code}": "角色修改/刪除（permissions_admin.py 自審）",
    "/api/portal/students/{student_id}/reveal-phone": "揭露監護人電話（portal/students.py 同交易自審）",
    # ── 登入者自助（教師/家長操作自己的資料）──
    "/api/portal/profile": "教師改自己 profile（自助）",
    "/api/portal/profile/line-binding": "教師綁/解自己 LINE（自助）",
    "/api/portal/my-punch-corrections": "教師送出自己的補打卡申請（核准端 /api/punch-corrections 已稽核）",
    "/api/parent/activity/register": "家長自助才藝報名（報名資料於才藝系統可查）",
    "/api/parent/activity/registrations/{registration_id}/confirm-promotion": "家長自助確認候補轉正",
    # ── 家長端認證 ──
    "/api/parent/auth/bind": "家長綁定（bind token）",
    "/api/parent/auth/bind-additional": "家長追加綁定",
    "/api/parent/auth/device-setup": "家長裝置設定（token）",
    "/api/parent/auth/liff-login": "家長 LIFF 登入（認證端點）",
    "/api/parent/auth/logout": "家長登出（自助）",
    "/api/parent/auth/refresh": "家長 token 刷新",
    # ── 已讀回報 / 操作確認（高量、低稽核價值）──
    "/api/portal/announcements/{announcement_id}/read": "公告已讀回報（量大）",
    "/api/parent/announcements/{announcement_id}/read": "家長公告已讀回報（量大）",
    "/api/portal/class-attendance/batch": "教師日常批次點名（量大；爭議性覆寫另走顯式稽核）",
    "/api/portal/anomalies/{attendance_id}/confirm": "教師確認考勤異常（操作性）",
    "/api/portal/dismissal-calls/{call_id}/acknowledge": "教師確認接送（操作性；發起/取消端已稽核）",
    "/api/portal/dismissal-calls/{call_id}/complete": "教師完成接送（操作性）",
    # ── 唯讀預覽 / 排程手動觸發 / meta（無業務資料異動或屬重算）──
    "/api/bonus-impact-preview": "獎金影響試算（唯讀預覽，不寫 DB）",
    "/api/leave-quota-expiry/run-now": "假別額度到期排程手動觸發（冪等批次）",
    "/api/gov-moe/monthly/generate": "教育部月報產生（可重跑報表，無 PII 異動）",
    "/api/audit-logs/ack-all": "確認 audit 告警（meta，避免遞迴稽核）",
    "/api/audit-logs/{audit_id}/ack": "確認單筆 audit 告警（meta）",
}


# 宣告式稽核偵測（MID-1）：audit_entity(...) 回傳 closure 的 __qualname__ 帶外層工廠名
# 'audit_entity.<locals>._set_audit_entity_type'，以子字串比對辨識（同 mutation_guard 模板）。
_AUDIT_ENTITY_MARKER = "audit_entity"


def _collect_dependency_qualnames(dependant, acc: list[str]) -> None:
    for sub in dependant.dependencies:
        call = getattr(sub, "call", None)
        if call is not None:
            acc.append(getattr(call, "__qualname__", str(call)))
        _collect_dependency_qualnames(sub, acc)


def _route_has_audit_entity_dep(route: APIRoute) -> bool:
    """route 是否掛宣告式 Depends(audit_entity(...))（router-level 或端點 level 皆可）。"""
    quals: list[str] = []
    if route.dependant is not None:
        _collect_dependency_qualnames(route.dependant, quals)
    return any(_AUDIT_ENTITY_MARKER in q for q in quals)


def _uncovered_mutation_paths() -> set[str]:
    """所有『不會被 AuditMiddleware 稽核』的 mutation 端點 path。

    覆蓋來源有二：① ENTITY_PATTERNS path 比對（_parse_entity_type）② 宣告式
    Depends(audit_entity(...))（MID-1，把『該端點稽核什麼』寫在端點旁）。兩者皆無
    → 視為未稽核，須在 AUDIT_EXEMPT 顯式豁免。
    """
    paths: set[str] = set()
    for r in main.app.routes:
        if not isinstance(r, APIRoute):
            continue
        if not ((r.methods or set()) & _MUTATING):
            continue
        if _parse_entity_type(r.path) is not None:
            continue
        if _route_has_audit_entity_dep(r):
            continue
        paths.add(r.path)
    return paths


def test_no_unaudited_mutation_endpoint():
    """每個 mutation 端點都須被 ENTITY_PATTERNS 覆蓋，或明確列入 AUDIT_EXEMPT。"""
    uncovered = _uncovered_mutation_paths()
    new_unaudited = sorted(uncovered - set(AUDIT_EXEMPT))
    assert not new_unaudited, (
        "以下 mutation 端點不會落 audit_logs（_parse_entity_type 回 None）且不在 "
        "AUDIT_EXEMPT 白名單：\n"
        + "\n".join(f"  - {p}" for p in new_unaudited)
        + "\n修法：在 utils/audit.py ENTITY_PATTERNS 補 (regex, entity_type)（並在 "
        "ENTITY_LABELS 補中文 label）；若刻意不稽核（公開/認證/自助/端點自審/高量/"
        "唯讀預覽），請加進本檔 AUDIT_EXEMPT 並寫理由。"
    )


def test_audit_exempt_has_no_stale_entries():
    """白名單不得有過時項：每個 AUDIT_EXEMPT key 都應仍是一個『未被稽核的』
    mutation 端點（端點已補 pattern 或被移除時，提醒清掉白名單）。"""
    uncovered = _uncovered_mutation_paths()
    stale = sorted(set(AUDIT_EXEMPT) - uncovered)
    assert not stale, (
        "以下 AUDIT_EXEMPT 白名單項已不再是『未被稽核的 mutation 端點』"
        "（已補 pattern 或已移除），請從白名單刪除：\n"
        + "\n".join(f"  - {p}" for p in stale)
    )
