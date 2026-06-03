"""normalize academic_terms dates + silent is_current reconcile

學期改為日期自動推導後：把所有 academic_terms 的 start_date/end_date 正規化成固定值
（上學期 8/1–隔年1/31、下學期 2/1–7/31），並把 is_current 靜默對齊到「今天日期推導」
的學期（缺則建立）。**不觸發任何 term.changed 事件**——純資料遷移，避免上線誤觸發批次結轉。

Revision ID: acadterm01
Revises: mergeheads10
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "acadterm01"
down_revision = "mergeheads10"
branch_labels = None
depends_on = None


def _bounds(school_year: int, semester: int):
    base = school_year + 1911
    if semester == 1:
        return date(base, 8, 1), date(base + 1, 1, 31)
    return date(base + 1, 2, 1), date(base + 1, 7, 31)


def _resolve_by_date(d: date):
    if d.month >= 8:
        return d.year - 1911, 1
    if d.month >= 2:
        return d.year - 1 - 1911, 2
    return d.year - 1 - 1911, 1


def upgrade():
    bind = op.get_bind()
    terms = sa.table(
        "academic_terms",
        sa.column("id", sa.Integer),
        sa.column("school_year", sa.Integer),
        sa.column("semester", sa.Integer),
        sa.column("start_date", sa.Date),
        sa.column("end_date", sa.Date),
        sa.column("is_current", sa.Boolean),
    )

    # 1) 正規化所有 row 的起訖日
    rows = bind.execute(
        sa.select(terms.c.id, terms.c.school_year, terms.c.semester)
    ).fetchall()
    for rid, sy, sem in rows:
        start, end = _bounds(sy, sem)
        bind.execute(
            sa.update(terms)
            .where(terms.c.id == rid)
            .values(start_date=start, end_date=end)
        )

    # 2) 靜默對齊 is_current 到今天日期推導的學期（缺則建立）；【不觸發事件】
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    tsy, tsem = _resolve_by_date(today)

    # 先全部清 is_current（避免 partial unique 衝突），再設目標
    bind.execute(sa.update(terms).values(is_current=False))

    target = bind.execute(
        sa.select(terms.c.id).where(
            terms.c.school_year == tsy, terms.c.semester == tsem
        )
    ).first()
    if target is None:
        start, end = _bounds(tsy, tsem)
        bind.execute(
            sa.insert(terms).values(
                school_year=tsy,
                semester=tsem,
                start_date=start,
                end_date=end,
                is_current=True,
            )
        )
    else:
        bind.execute(
            sa.update(terms).where(terms.c.id == target[0]).values(is_current=True)
        )


def downgrade():
    # 純資料正規化，無法精確還原使用者原本自訂的任意起訖日；no-op。
    pass
