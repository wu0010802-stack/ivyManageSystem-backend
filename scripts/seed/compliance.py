"""scripts/seed/compliance.py — 個資合規模組示範資料 seed。

模組範圍（個資法 §3 / §8 / §19）:
- parent_consent_log:家長同意 / 撤回事件 log（consent 的單一來源；scope-aware）
- dsr_requests:資料主體請求（delete / correct / opt_out × pending / approved / rejected）

FK 前置:parent_consent_log.policy_version_id 為 NOT NULL，故本腳本會先冪等
建立一筆 PolicyVersion（exists-check on version 字串），再寫 consent log。

冪等契約:
- 每筆插入前先 exists 查（key 為穩定欄位，不含隨機日期）。
- 重跑必須新增 0 筆、不刪改現有資料。
- 不建立 / 不修改任何 User、Guardian、Student（避免動現有資料 + 避免孤兒帳號）。

⚠ 設計取捨（schema 現實 vs 任務「~30 guardian」建議量）:
parent_consent_log 與 dsr_requests **皆以 user_id（家長/員工 User）為主體鍵**，
無 guardian_id / student_id 欄位。dev DB 僅 4 個 parent User，且冪等契約禁止
改寫既有 guardians.user_id 來新增主體。故 consent 取樣鎖在 4 個真實 parent User
× 4 個合法 scope，以「同意→撤回→再同意」歷程產生多筆 row（~20+ 筆 log），
真實呈現 history 功能；而非臆造 30 個不存在的主體。詳見回報。

日期界線:2025-08-01 ~ 2026-06-05，絕不生未來。

用法:
    cd ~/Desktop/ivy-backend
    python3 -m scripts.seed.compliance
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from models.auth import User
from models.consent import (
    CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
    CONSENT_SCOPE_LINE_PUSH,
    CONSENT_SCOPE_PHOTO_PUBLISH,
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_REQUEST_TYPE_OPT_OUT,
    DSR_STATUS_APPROVED,
    DSR_STATUS_PENDING,
    DSR_STATUS_REJECTED,
    DsrRequest,
)

from scripts.seed._common import (
    get_admin_user,
    session_scope,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed_compliance")

# ===== 日期界線（絕不生未來；上限 = 今天）=====
LOWER = date(2025, 8, 1)
UPPER = date(2026, 6, 5)

# 政策版本（consent log FK 前置；exists-check on version 字串）
POLICY_VERSION = "2025.1"
POLICY_EFFECTIVE = datetime(2025, 8, 1, 9, 0)


def _dt(d: date, hour: int = 10) -> datetime:
    """date → 落在界線內的固定 datetime（不用隨機，確保重跑 exists-key 穩定）。"""
    return datetime(d.year, d.month, d.day, hour, 0)


def _ensure_policy_version(session) -> PolicyVersion:
    """冪等建立一筆生效中的 PolicyVersion（consent log 的 NOT NULL FK 前置）。"""
    pv = (
        session.query(PolicyVersion)
        .filter(PolicyVersion.version == POLICY_VERSION)
        .first()
    )
    if pv is not None:
        return pv
    pv = PolicyVersion(
        version=POLICY_VERSION,
        effective_at=POLICY_EFFECTIVE,
        document_path="policies/privacy-2025.1.html",
        summary="114 學年隱私權政策（示範 seed）。",
    )
    session.add(pv)
    session.flush()
    logger.info("已建立 PolicyVersion version=%s id=%s", POLICY_VERSION, pv.id)
    return pv


def _get_parent_users(session) -> list[User]:
    """取所有 role=parent 的 User（consent log 主體）。"""
    return session.query(User).filter(User.role == "parent").order_by(User.id).all()


def _seed_consent_log(session, policy_id: int) -> int:
    """為各 parent User × scope 寫入同意 / 撤回歷程。

    每個 (user_id, scope, consented_at) 為穩定 exists-key（日期固定不隨機）。
    歷程設計（部分同意、部分拒絕、部分撤回後再同意）:
      - service_essential:全部同意（基礎服務必要）
      - photo_publish:同意 → 之後撤回（部分家長）
      - line_push:部分同意、部分一開始就拒絕
      - cross_border_transfer:多數拒絕（敏感）；少數同意後又撤回
    """
    parents = _get_parent_users(session)
    if not parents:
        logger.warning("dev DB 無 parent User，consent log 略過")
        return 0

    inserted = 0
    # 每筆 = (scope, consented, day) 的固定歷程模板，依 parent 序位輪換出多樣性。
    # day 落在 2025-08 ~ 2026-06 之間，皆 <= UPPER。
    templates = [
        # service_essential:全員同意（首次登入即簽）
        (CONSENT_SCOPE_SERVICE_ESSENTIAL, True, date(2025, 8, 15)),
        # photo_publish:先同意
        (CONSENT_SCOPE_PHOTO_PUBLISH, True, date(2025, 9, 1)),
        # line_push:先同意
        (CONSENT_SCOPE_LINE_PUSH, True, date(2025, 9, 10)),
    ]
    # 進階歷程（依 parent index 決定是否套用，製造「部分同意/部分拒絕/撤回再同意」）
    for idx, user in enumerate(parents):
        rows: list[tuple[str, bool, date]] = list(templates)

        # 偶數序位家長:跨境傳輸拒絕（敏感資料多數不同意）
        if idx % 2 == 0:
            rows.append((CONSENT_SCOPE_CROSS_BORDER_TRANSFER, False, date(2025, 9, 20)))
        else:
            # 奇數序位:跨境先同意，後撤回（demonstrate 撤回歷程）
            rows.append((CONSENT_SCOPE_CROSS_BORDER_TRANSFER, True, date(2025, 9, 20)))
            rows.append((CONSENT_SCOPE_CROSS_BORDER_TRANSFER, False, date(2026, 1, 10)))

        # 第 0 位家長:照片同意後撤回 → 再同意（完整 consent→revoke→re-consent 歷程）
        if idx == 0:
            rows.append((CONSENT_SCOPE_PHOTO_PUBLISH, False, date(2025, 12, 5)))
            rows.append((CONSENT_SCOPE_PHOTO_PUBLISH, True, date(2026, 3, 1)))
        # 第 1 位家長:LINE 推播後撤回（不想收通知）
        if idx == 1:
            rows.append((CONSENT_SCOPE_LINE_PUSH, False, date(2026, 2, 15)))

        for scope, consented, day in rows:
            assert LOWER <= day <= UPPER, f"日期越界:{day}"
            consented_at = _dt(day)
            exists = (
                session.query(ParentConsentLog.id)
                .filter(
                    ParentConsentLog.user_id == user.id,
                    ParentConsentLog.scope == scope,
                    ParentConsentLog.consented_at == consented_at,
                )
                .first()
            )
            if exists is not None:
                continue
            log = ParentConsentLog(
                user_id=user.id,
                policy_version_id=policy_id,
                scope=scope,
                consented=consented,
                consented_at=consented_at,
                note=("家長自助撤回" if not consented else None),
            )
            session.add(log)
            inserted += 1

    return inserted


def _seed_dsr_requests(session) -> int:
    """寫入 ~8 筆 DSR 請求，涵蓋 3 種 request_type × 3 種 status。

    exists-key:(user_id, request_type, subject_entity_type, subject_entity_id,
    scope, submitted_at) 的穩定組合（日期固定不隨機）。
    申請人關聯到既有 parent User；subject_entity 指向既有 student / guardian。
    """
    parents = _get_parent_users(session)
    admin = get_admin_user(session)
    admin_id = admin.id if admin is not None else None
    if not parents:
        logger.warning("dev DB 無 parent User，DSR 略過")
        return 0

    # 以第一個 parent（durable user 5，子女 = student 1，guardians 1733/1734）為主申請人，
    # 其餘 parent 補幾筆以增多樣性。所有 subject_entity 皆指向真實 entity。
    p0 = parents[0]
    p_extra = parents[1] if len(parents) > 1 else parents[0]
    p_extra2 = parents[2] if len(parents) > 2 else parents[0]

    # (user, request_type, status, subject_type, subject_id, field_name, new_value,
    #  scope, reason, submitted_day, decided_day, decision_note)
    specs = [
        # delete × pending（待 admin 審）
        (
            p0,
            DSR_REQUEST_TYPE_DELETE,
            DSR_STATUS_PENDING,
            "student",
            1,
            None,
            None,
            None,
            "孩子已轉學，申請刪除在校個資。",
            date(2026, 5, 20),
            None,
            None,
        ),
        # delete × rejected（稅務保存義務駁回）
        (
            p_extra,
            DSR_REQUEST_TYPE_DELETE,
            DSR_STATUS_REJECTED,
            "guardian",
            1727,
            None,
            None,
            None,
            "申請刪除家長聯絡資料。",
            date(2026, 3, 10),
            date(2026, 3, 14),
            "依稅法須保存學費繳費紀錄 7 年，暫不可刪除，到期後自動清除。",
        ),
        # correct × pending（更正電話，待審）
        (
            p0,
            DSR_REQUEST_TYPE_CORRECT,
            DSR_STATUS_PENDING,
            "guardian",
            1733,
            "phone",
            "0912345678",
            None,
            "聯絡電話已變更，請更正。",
            date(2026, 5, 28),
            None,
            None,
        ),
        # correct × approved（更正地址，已核准）
        (
            p_extra,
            DSR_REQUEST_TYPE_CORRECT,
            DSR_STATUS_APPROVED,
            "student",
            99401,
            "address",
            "新北市板橋區文化路一段100號",
            None,
            "搬家，更新通訊地址。",
            date(2026, 2, 5),
            date(2026, 2, 7),
            "已核准，行政人員手動更新地址欄位。",
        ),
        # correct × rejected（要求改姓名但無證明）
        (
            p_extra2,
            DSR_REQUEST_TYPE_CORRECT,
            DSR_STATUS_REJECTED,
            "guardian",
            1728,
            "name",
            "新姓名",
            None,
            "更正監護人姓名。",
            date(2026, 1, 15),
            date(2026, 1, 18),
            "更名須附戶政證明文件，請補件後重新申請。",
        ),
        # opt_out × approved（撤回照片公開；router 實務上即時 approved）
        (
            p0,
            DSR_REQUEST_TYPE_OPT_OUT,
            DSR_STATUS_APPROVED,
            None,
            None,
            None,
            None,
            CONSENT_SCOPE_PHOTO_PUBLISH,
            "不希望孩子照片刊登於園所社群。",
            date(2026, 4, 12),
            date(2026, 4, 12),
            "自助撤回即時生效，consent_log 已記 consented=false。",
        ),
        # opt_out × approved（撤回 LINE 推播）
        (
            p_extra,
            DSR_REQUEST_TYPE_OPT_OUT,
            DSR_STATUS_APPROVED,
            None,
            None,
            None,
            None,
            CONSENT_SCOPE_LINE_PUSH,
            "停止接收 LINE 推播通知。",
            date(2026, 2, 16),
            date(2026, 2, 16),
            "自助撤回即時生效。",
        ),
        # opt_out × approved（撤回跨境傳輸）
        (
            p_extra2,
            DSR_REQUEST_TYPE_OPT_OUT,
            DSR_STATUS_APPROVED,
            None,
            None,
            None,
            None,
            CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
            "不同意個資跨境傳輸。",
            date(2025, 11, 3),
            date(2025, 11, 3),
            "自助撤回即時生效。",
        ),
    ]

    inserted = 0
    for (
        user,
        request_type,
        status,
        subject_type,
        subject_id,
        field_name,
        new_value,
        scope,
        reason,
        submitted_day,
        decided_day,
        decision_note,
    ) in specs:
        assert LOWER <= submitted_day <= UPPER, f"submitted 越界:{submitted_day}"
        submitted_at = _dt(submitted_day)
        # exists-key:穩定欄位組合（不含隨機）
        q = session.query(DsrRequest.id).filter(
            DsrRequest.user_id == user.id,
            DsrRequest.request_type == request_type,
            DsrRequest.submitted_at == submitted_at,
        )
        if subject_type is None:
            q = q.filter(DsrRequest.subject_entity_type.is_(None))
        else:
            q = q.filter(DsrRequest.subject_entity_type == subject_type)
        if scope is None:
            q = q.filter(DsrRequest.scope.is_(None))
        else:
            q = q.filter(DsrRequest.scope == scope)
        if q.first() is not None:
            continue

        decided_at = None
        decided_by = None
        if status != DSR_STATUS_PENDING:
            # 已決議:decided_at 須 >= submitted_at 且 <= 今天
            dday = decided_day or submitted_day
            assert submitted_day <= dday <= UPPER, f"decided 越界:{dday}"
            decided_at = _dt(dday, hour=14)
            decided_by = admin_id

        req = DsrRequest(
            user_id=user.id,
            request_type=request_type,
            status=status,
            subject_entity_type=subject_type,
            subject_entity_id=subject_id,
            field_name=field_name,
            new_value=new_value,
            scope=scope,
            reason=reason,
            submitted_at=submitted_at,
            decided_at=decided_at,
            decided_by=decided_by,
            decision_note=decision_note,
        )
        session.add(req)
        inserted += 1

    return inserted


def step() -> None:
    """灌個資合規模組示範資料（冪等）。"""
    with session_scope() as session:
        policy = _ensure_policy_version(session)
        consent_n = _seed_consent_log(session, policy.id)
        dsr_n = _seed_dsr_requests(session)

    # 交易已 commit；另開 session 取真實表內總筆數（避免在同 session 內 count
    # 到尚未 flush 的 pending rows 造成重複計數）。
    with session_scope() as session:
        total_consent = session.query(ParentConsentLog).count()
        total_dsr = session.query(DsrRequest).count()

    logger.info(
        "parent_consent_log:本次新增 %d 筆（表內共 %d）",
        consent_n,
        total_consent,
    )
    logger.info(
        "dsr_requests:本次新增 %d 筆（表內共 %d）",
        dsr_n,
        total_dsr,
    )
    logger.info("個資合規 seed 完成。")


if __name__ == "__main__":
    step()
