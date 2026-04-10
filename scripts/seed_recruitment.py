"""
scripts/seed_recruitment.py — 一次性匯入招生訪視記錄（114.02 ～ 115.03）

執行方式（在 backend/ 目錄下）：
    python scripts/seed_recruitment.py
"""

import json
import sys
import os
from datetime import date
from pathlib import Path

# 確保可以 import backend 模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import models.database  # noqa: 確保所有 Table 向 Base.metadata 登記
from models.base import Base
from models.database import init_database
from models.recruitment import RecruitmentVisit


def parse_roc_date(s: str | None) -> date | None:
    """解析民國日期字串（如 '112.01.13'）為西元 date"""
    if not s:
        return None
    try:
        parts = s.strip().split(".")
        if len(parts) == 3:
            year = int(parts[0]) + 1911
            return date(year, int(parts[1]), int(parts[2]))
    except (ValueError, AttributeError):
        pass
    return None


def main():
    data_file = Path(__file__).parent / "recruitment_seed_data.json"
    if not data_file.exists():
        print(f"找不到資料檔：{data_file}")
        sys.exit(1)

    with open(data_file, encoding="utf-8") as f:
        records = json.load(f)

    print(f"載入 {len(records)} 筆資料...")

    engine, SessionLocal = init_database()
    # 確保 recruitment_visits 資料表存在（只建立尚未存在的表）
    RecruitmentVisit.__table__.create(engine, checkfirst=True)
    session = SessionLocal()

    try:
        existing = set(
            (r.child_name, r.month)
            for r in session.query(RecruitmentVisit.child_name, RecruitmentVisit.month).all()
        )
        inserted = 0
        skipped = 0

        for rec in records:
            name = (rec.get("幼生姓名") or "").strip()
            month = (rec.get("月份") or "").strip()
            if not name or not month:
                skipped += 1
                continue
            if (name, month) in existing:
                skipped += 1
                continue

            visit = RecruitmentVisit(
                month=month,
                seq_no=rec.get("序號"),
                visit_date=rec.get("日期"),
                child_name=name,
                birthday=parse_roc_date(rec.get("生日")),
                grade=rec.get("適讀班級"),
                phone=rec.get("電話"),
                address=rec.get("地址"),
                district=rec.get("行政區"),
                source=rec.get("幼生來源"),
                referrer=rec.get("介紹者"),
                deposit_collector=rec.get("收預繳人員"),
                has_deposit=(rec.get("是否預繳") == "是"),
                notes=rec.get("備註"),
                parent_response=rec.get("電訪後家長回應"),
            )
            session.add(visit)
            existing.add((name, month))
            inserted += 1

        session.commit()
        print(f"✅ 匯入完成：插入 {inserted} 筆，跳過 {skipped} 筆")
    except Exception as e:
        session.rollback()
        print(f"❌ 匯入失敗：{e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
