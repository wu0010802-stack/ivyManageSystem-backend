"""m14_audit_misc:稽核/雜項補齊資料(各表少量 >0)。

涵蓋:
- 稽核 log(audit_logs):操作者署名 + entity 操作摘要。
- 通知 log(notification_logs):in-app 通知 fan-out 結果(已讀/未讀)。
- 廠商付款(vendor_payments):清潔/教具/食材付款,含已簽收與待簽。
- 離職紀錄(employee_offboarding_records):對少量已離職員工建 checklist。
- 獎懲(disciplinary_actions):警告/小過/嘉獎/小功各少量(未抵扣)。
- 員工檔案(employee_contracts / employee_educations / employee_certificates)。
- 個資 DSR 申請(dsr_requests):刪除/更正/停止處理三型,含 pending/approved。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`ctx.config`(學年/today/closed_months)。
- m01:`ctx.employees`、`ctx.employees_by_role`、`ctx.users`
       (含 username='admin' 的 role='admin' User)。

時間規則:落點一律 ≤ config.today,不生 future。金額整數(NTD),
落入 Numeric/Money 欄位合法。所有亂數走 ctx.rng,決定論可重現;
naive datetime 對齊既有欄位(now_taipei_naive)語意。

職責定位:本模組為「雜項補齊」,各表只求 >0 與語意/FK 合法,不追求量,
亦不重算薪資(disciplinary 一律 applied_to_salary_id=NULL 視為未抵扣,
避免污染 m06 已算之 salary_records)。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from models.audit import AuditLog
from models.disciplinary import (
    ACTION_TYPE_COMMEND,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_MINOR_MERIT,
    ACTION_TYPE_WARNING,
    DisciplinaryAction,
)
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_REQUEST_TYPE_OPT_OUT,
    DSR_STATUS_APPROVED,
    DSR_STATUS_PENDING,
    DsrRequest,
)
from models.employee import (
    EmployeeCertificate,
    EmployeeContract,
    EmployeeEducation,
)
from models.notification_log import NotificationLog
from models.offboarding import EmployeeOffboardingRecord
from models.vendor_payment import VendorPayment

from ..context import SeedContext
from ..fake import Faker

# 廠商付款範本(vendor_name, payment_method, description)。
_VENDOR_TEMPLATES: list[tuple[str, str, str]] = [
    ("快樂清潔行", "bank_transfer", "園所每月清潔用品補給"),
    ("童心教具社", "check", "幼兒教具與美勞材料"),
    ("新鮮食材行", "cash", "幼兒餐點食材採購"),
    ("安心消防工程", "bank_transfer", "消防設備年度檢修"),
    ("綠意園藝", "cash", "戶外綠化與草皮維護"),
    ("明亮文具批發", "linepay", "辦公與教學文具"),
]

# 稽核 log 範本(action, entity_type, summary)。
_AUDIT_TEMPLATES: list[tuple[str, str, str]] = [
    ("CREATE", "employee", "新增員工資料"),
    ("UPDATE", "student", "更新學生聯絡資訊"),
    ("UPDATE", "salary_record", "調整薪資加項"),
    ("DELETE", "announcement", "刪除過期公告"),
    ("CREATE", "leave_record", "代為登錄請假單"),
    ("UPDATE", "fee_record", "更新繳費狀態"),
]

# 通知 log 範本(event_type, title, body)。
_NOTIF_TEMPLATES: list[tuple[str, str, str]] = [
    ("leave_approved", "請假已核准", "您的請假申請已經主管核准。"),
    ("salary_published", "薪資單已發布", "本月薪資明細已可於系統查閱。"),
    ("announcement_new", "新公告通知", "園所發布了一則新公告,請查閱。"),
    ("appraisal_open", "考核開放填寫", "本期考核已開放,請於期限內完成。"),
]

# 員工合約類型(對齊 contract_type 註解:正式/兼職/試用/臨時/續約)。
_CONTRACT_TYPES: list[str] = ["正式", "試用", "兼職", "續約"]

# 學歷學位(對齊 degree 註解:高中職/學士/碩士/博士/其他)。
_DEGREES: list[tuple[str, str]] = [
    ("學士", "幼兒教育學系"),
    ("學士", "幼兒保育系"),
    ("碩士", "教育研究所"),
    ("高中職", "幼兒保育科"),
]

# 證照範本(certificate_name, issuer)。
_CERTIFICATES: list[tuple[str, str]] = [
    ("教保員資格證書", "教育部"),
    ("保母人員技術士證", "勞動部"),
    ("CPR 急救證照", "紅十字會"),
    ("幼兒園教師證", "教育部"),
]


def _naive_dt(d: date, hour: int = 9, minute: int = 0) -> datetime:
    """以日期 + 時分組出 naive datetime(對齊既有欄位語意)。"""
    return datetime.combine(d, time(hour=hour, minute=minute))


def _clamp_day(d: date, today: date) -> date:
    """確保日期不超過 today(避免生成 future 落點)。"""
    return d if d <= today else today


def _admin_user(ctx: SeedContext):
    """取一個 role='admin' 的 User(操作者署名 / DSR 處理者用);無則任意 User。"""
    users = ctx.users or {}
    admin = users.get("admin")
    if admin is not None and getattr(admin, "role", None) == "admin":
        return admin
    for user in users.values():
        if getattr(user, "role", None) == "admin":
            return user
    # fallback:任意 User(理論上 m01 必有 admin)。
    return next(iter(users.values()), None)


def _seed_audit_logs(ctx: SeedContext, admin_user) -> int:
    """建少量稽核 log:操作者為 admin,涵蓋 CREATE/UPDATE/DELETE。"""
    session = ctx.session
    today = ctx.config.today
    uid = admin_user.id if admin_user is not None else None
    uname = getattr(admin_user, "username", None) if admin_user is not None else None

    n = 0
    for i, (action, entity_type, summary) in enumerate(_AUDIT_TEMPLATES):
        # 落點散佈於 today 前 1~60 天的工作時段。
        offset = ctx.rng.randint(1, 60)
        created = _naive_dt(_clamp_day(today - timedelta(days=offset), today), hour=10)
        log = AuditLog(
            user_id=uid,
            username=uname,
            action=action,
            entity_type=entity_type,
            entity_id=str(ctx.rng.randint(1, 200)),
            summary=summary,
            changes=None,
            ip_address="127.0.0.1",
            created_at=created,
            session_id=f"seed-jti-{i:03d}",
        )
        session.add(log)
        n += 1
    return n


def _seed_notification_logs(ctx: SeedContext, admin_user) -> int:
    """建少量 in-app 通知 log:收件者為各員工帳號 User,部分已讀。"""
    session = ctx.session
    today = ctx.config.today
    sender_id = admin_user.id if admin_user is not None else None
    # 收件者用已建 User(recipient_user_id NOT NULL + FK users)。
    recipients = [u for u in (ctx.users or {}).values() if getattr(u, "id", None)]
    if not recipients:
        return 0

    n = 0
    for i, (event_type, title, body) in enumerate(_NOTIF_TEMPLATES):
        recipient = recipients[i % len(recipients)]
        offset = ctx.rng.randint(1, 30)
        created = _naive_dt(_clamp_day(today - timedelta(days=offset), today), hour=14)
        # 半數標為已讀(read_at 設於建立後同日;in-app channel 成功)。
        is_read = (i % 2) == 0
        read_at = _naive_dt(created.date(), hour=16) if is_read else None
        log = NotificationLog(
            recipient_user_id=recipient.id,
            event_type=event_type,
            sender_id=sender_id,
            title=title,
            body=body,
            payload_json={},
            source_entity_type=None,
            source_entity_id=None,
            deep_link=None,
            channels_attempted=["in_app"],
            channels_succeeded=["in_app"],
            channels_failed=[],
            line_retry_count=0,
            is_inbox_visible=True,
            read_at=read_at,
            created_at=created,
        )
        session.add(log)
        n += 1
    return n


def _seed_vendor_payments(ctx: SeedContext, faker: Faker, admin_emp, signer_emp) -> int:
    """建少量廠商付款:多數已簽收(signed),少量待簽(pending)。"""
    session = ctx.session
    today = ctx.config.today
    creator_id = admin_emp.id if admin_emp is not None else None
    signer_id = signer_emp.id if signer_emp is not None else None

    n = 0
    for i, (vendor_name, method, desc) in enumerate(_VENDOR_TEMPLATES):
        offset = ctx.rng.randint(3, 90)
        pay_date = _clamp_day(today - timedelta(days=offset), today)
        amount = faker.amount(1500, 35000, step=100)
        # 最後一筆留 pending(未簽收)以涵蓋兩種 status。
        is_signed = i < len(_VENDOR_TEMPLATES) - 1 and signer_id is not None
        vp = VendorPayment(
            payment_date=pay_date,
            vendor_name=vendor_name,
            amount=amount,
            payment_method=method,
            description=desc,
            invoice_number=f"INV-{pay_date.strftime('%Y%m')}-{i:03d}",
            notes=None,
            attachments=[],
            status="signed" if is_signed else "pending",
            signer_id=signer_id if is_signed else None,
            signed_at=_naive_dt(pay_date, hour=11) if is_signed else None,
            signature_kind="drawn" if is_signed else None,
            signature_key=None,
            created_by_id=creator_id,
        )
        session.add(vp)
        n += 1
    return n


def _seed_disciplinary(ctx: SeedContext) -> int:
    """建少量獎懲紀錄:警告/小過/嘉獎/小功,皆未抵扣(不污染薪資)。

    deduction_amount=0 表示「用 BonusConfig 預設」;applied_to_salary_id=NULL
    表示尚未抵扣——避免動到 m06 已算的 salary_records。
    """
    session = ctx.session
    today = ctx.config.today
    employees = ctx.employees or []
    if not employees:
        return 0

    # 取 4 名不同員工各掛一筆(數量少,僅求涵蓋懲處/merit 兩類)。
    types = [
        (ACTION_TYPE_WARNING, "上班遲到累計達標,予以警告。"),
        (ACTION_TYPE_MINOR, "未依規定請假,記小過一次。"),
        (ACTION_TYPE_COMMEND, "協助園所活動表現優異,予以嘉獎。"),
        (ACTION_TYPE_MINOR_MERIT, "主動處理突發狀況,記小功一次。"),
    ]
    n = 0
    for i, (action_type, reason) in enumerate(types):
        emp = employees[i % len(employees)]
        offset = ctx.rng.randint(10, 120)
        action_date = _clamp_day(today - timedelta(days=offset), today)
        da = DisciplinaryAction(
            employee_id=emp.id,
            action_date=action_date,
            action_type=action_type,
            deduction_amount=0,
            reason=reason,
            applied_to_salary_id=None,
            applied_at=None,
            applied_amount=None,
            created_by="admin",
            updated_by="admin",
        )
        session.add(da)
        n += 1
    return n


def _seed_employee_profile(ctx: SeedContext, faker: Faker) -> tuple[int, int, int]:
    """為前幾名員工建合約/學歷/證照各一筆,回傳 (合約, 學歷, 證照) 筆數。"""
    session = ctx.session
    employees = ctx.employees or []
    if not employees:
        return (0, 0, 0)

    # 取最多前 6 名員工建檔(少量即可)。
    sample = employees[: min(6, len(employees))]
    n_contract = n_edu = n_cert = 0
    for i, emp in enumerate(sample):
        hire = getattr(emp, "hire_date", None) or ctx.config.year_start
        base = getattr(emp, "base_salary", None) or 0

        # 合約:起日對齊到職日,正式合約多數無結束日。
        contract_type = _CONTRACT_TYPES[i % len(_CONTRACT_TYPES)]
        session.add(
            EmployeeContract(
                employee_id=emp.id,
                contract_type=contract_type,
                start_date=hire,
                end_date=None,
                salary_at_contract=base,
                remark=None,
            )
        )
        n_contract += 1

        # 學歷:取一筆並標為最高學歷。
        degree, major = _DEGREES[i % len(_DEGREES)]
        grad = faker.birthday(min_age=2, max_age=8, ref=hire)
        session.add(
            EmployeeEducation(
                employee_id=emp.id,
                school_name=f"{ctx.rng.choice(['國立', '私立'])}幼兒教育大學",
                major=major,
                degree=degree,
                graduation_date=grad,
                is_highest=True,
                remark=None,
            )
        )
        n_edu += 1

        # 證照:取一筆,部分設到期日。
        cert_name, issuer = _CERTIFICATES[i % len(_CERTIFICATES)]
        issued = faker.birthday(min_age=1, max_age=6, ref=hire)
        expiry = None
        if (i % 2) == 0:
            # 用 +5 年(以天數位移避免閏日 2/29 在非閏年 ValueError)。
            expiry = issued + timedelta(days=365 * 5)
        session.add(
            EmployeeCertificate(
                employee_id=emp.id,
                certificate_name=cert_name,
                issuer=issuer,
                certificate_number=f"CERT-{emp.id}-{i:02d}",
                issued_date=issued,
                expiry_date=expiry,
                remark=None,
            )
        )
        n_cert += 1
    return (n_contract, n_edu, n_cert)


def _seed_offboarding(ctx: SeedContext, admin_user) -> int:
    """為少量已離職員工建離職 checklist(one-to-one,PK=employee_id)。

    優先挑 is_active=False 且有 resign_date 的員工;若 m01 未產生離職員工,
    則退而取最後一名員工建一筆(resign_date 落 today,closed checklist)。
    """
    session = ctx.session
    today = ctx.config.today
    employees = ctx.employees or []
    if not employees:
        return 0
    opener_uid = admin_user.id if admin_user is not None else None
    if opener_uid is None:
        return 0  # opened_by_user_id NOT NULL,無 admin 則跳過

    # 候選:已離職員工。
    resigned = [
        e
        for e in employees
        if getattr(e, "is_active", True) is False
        and getattr(e, "resign_date", None) is not None
    ]
    candidates = resigned[:2]
    if not candidates:
        # fallback:取最後一名員工模擬一筆離職(不改其 is_active,僅建紀錄)。
        candidates = [employees[-1]]

    n = 0
    for emp in candidates:
        resign_date = getattr(emp, "resign_date", None)
        if resign_date is None or resign_date > today:
            resign_date = _clamp_day(
                today - timedelta(days=ctx.rng.randint(5, 40)), today
            )
        opened = _naive_dt(resign_date, hour=9)
        # 視為已完成的離職流程(closed checklist)。
        rec = EmployeeOffboardingRecord(
            employee_id=emp.id,
            resign_date=resign_date,
            resign_reason="個人生涯規劃",
            opened_at=opened,
            opened_by_user_id=opener_uid,
            user_revoked_at=opened,
            appraisal_marked_at=opened,
            leave_snapshot_at=opened,
            certificate_generated_at=opened,
            leave_balance_snapshot={"annual_remaining_hours": 0},
            certificate_pdf_path=None,
            nhi_unenroll_submitted_at=opened,
            magic_link_download_count=0,
            closed_at=_naive_dt(resign_date, hour=17),
            closed_by_user_id=opener_uid,
        )
        session.add(rec)
        n += 1
    return n


def _seed_dsr_requests(ctx: SeedContext, admin_user) -> int:
    """建少量 DSR 申請:delete/correct/opt_out 三型,含 pending 與 approved。"""
    session = ctx.session
    today = ctx.config.today
    applicants = [u for u in (ctx.users or {}).values() if getattr(u, "id", None)]
    if not applicants:
        return 0
    decider_uid = admin_user.id if admin_user is not None else None

    # (request_type, status, subject_entity_type, field_name, scope, reason)
    specs: list[tuple[str, str, str, str | None, str | None, str]] = [
        (
            DSR_REQUEST_TYPE_DELETE,
            DSR_STATUS_PENDING,
            "guardian",
            None,
            None,
            "申請刪除個人聯絡資料。",
        ),
        (
            DSR_REQUEST_TYPE_CORRECT,
            DSR_STATUS_APPROVED,
            "student",
            "name",
            None,
            "更正學生姓名錯字。",
        ),
        (
            DSR_REQUEST_TYPE_OPT_OUT,
            DSR_STATUS_PENDING,
            "guardian",
            None,
            "marketing",
            "停止行銷類訊息處理。",
        ),
    ]
    n = 0
    for i, (req_type, status, subj_type, field_name, scope, reason) in enumerate(specs):
        applicant = applicants[i % len(applicants)]
        offset = ctx.rng.randint(2, 45)
        submitted = _naive_dt(
            _clamp_day(today - timedelta(days=offset), today), hour=13
        )
        is_decided = status == DSR_STATUS_APPROVED
        req = DsrRequest(
            user_id=applicant.id,
            request_type=req_type,
            status=status,
            subject_entity_type=subj_type,
            subject_entity_id=ctx.rng.randint(1, 200),
            field_name=field_name,
            new_value="王小明" if field_name == "name" else None,
            scope=scope,
            reason=reason,
            submitted_at=submitted,
            decided_at=_naive_dt(submitted.date(), hour=15) if is_decided else None,
            decided_by=decider_uid if is_decided else None,
            decision_note="已依個資法處理完成。" if is_decided else None,
            ip_address="127.0.0.1",
            user_agent=None,
        )
        session.add(req)
        n += 1
    return n


def seed(ctx: SeedContext) -> None:
    """建立稽核/雜項補齊資料(各表少量 >0,語意/FK 合法)。"""
    session = ctx.session
    if session is None:
        return

    faker = Faker(ctx.rng)
    admin_user = _admin_user(ctx)
    by_role = ctx.employees_by_role or {}
    admin_emp = (by_role.get("admin") or [None])[0]
    accountant_emp = (by_role.get("accountant") or [None])[0]
    signer_emp = (
        accountant_emp or admin_emp or (ctx.employees[0] if ctx.employees else None)
    )

    n_audit = _seed_audit_logs(ctx, admin_user)
    if n_audit:
        ctx.log("audit_logs", n_audit)

    n_notif = _seed_notification_logs(ctx, admin_user)
    if n_notif:
        ctx.log("notification_logs", n_notif)

    n_vendor = _seed_vendor_payments(ctx, faker, admin_emp, signer_emp)
    if n_vendor:
        ctx.log("vendor_payments", n_vendor)

    n_disc = _seed_disciplinary(ctx)
    if n_disc:
        ctx.log("disciplinary_actions", n_disc)

    n_contract, n_edu, n_cert = _seed_employee_profile(ctx, faker)
    if n_contract:
        ctx.log("employee_contracts", n_contract)
    if n_edu:
        ctx.log("employee_educations", n_edu)
    if n_cert:
        ctx.log("employee_certificates", n_cert)

    n_offboard = _seed_offboarding(ctx, admin_user)
    if n_offboard:
        ctx.log("employee_offboarding_records", n_offboard)

    n_dsr = _seed_dsr_requests(ctx, admin_user)
    if n_dsr:
        ctx.log("dsr_requests", n_dsr)
