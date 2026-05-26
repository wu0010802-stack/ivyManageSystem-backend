"""dr_drill_report.py — produce Markdown drill report.

Usage:
  python dr_drill_report.py \
    --dump-date 2026-05-26 \
    --start-ts 1716000000 --download-end-ts 1716000060 \
    --restore-end-ts 1716000300 \
    --sanity-output sanity_output.txt > drill-report.md
"""

import argparse
import re
from datetime import datetime, timezone


def parse_sanity(text: str) -> dict:
    """從 psql 輸出抽 row counts / latest_attendance / alembic_version。"""
    out: dict = {"row_counts": {}, "latest": {}, "alembic": ""}
    lines = text.splitlines()
    section = None
    for line in lines:
        if "Core table row counts" in line:
            section = "rows"
        elif "Latest event timestamps" in line:
            section = "latest"
        elif "Alembic head" in line:
            section = "alembic"
        elif "Cross-table join smoke" in line:
            section = "join"
        elif section == "rows":
            m = re.match(r"\s*(\w+)\s*\|\s*(\d+)", line)
            if m:
                out["row_counts"][m.group(1)] = int(m.group(2))
        elif section == "latest":
            m = re.match(r"\s*(\w+)\s*\|\s*(.+)", line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                # skip psql column-header rows (e.g. "check | value" or "----+----")
                if key == "check" or val == "value":
                    continue
                out["latest"][key] = val
        elif section == "alembic":
            m = re.match(r"\s*alembic_version\s*\|\s*(.+)", line)
            if m:
                out["alembic"] = m.group(1).strip()
    return out


def judge_pass(parsed: dict, dump_date: str) -> tuple[str, list[str]]:
    """回 (judgment, reasons)。"""
    warns: list[str] = []
    if not parsed["row_counts"]:
        warns.append("row_counts is empty — sanity SQL may have produced no results")
    for tbl, n in parsed["row_counts"].items():
        if n == 0:
            warns.append(f"{tbl} row count = 0")
    latest_att = parsed["latest"].get("latest_attendance", "")
    if latest_att and latest_att != "":
        try:
            d = datetime.fromisoformat(latest_att.replace("Z", "+00:00"))
            dump_d = datetime.fromisoformat(dump_date)
            if (dump_d - d.replace(tzinfo=None)).days > 2:
                warns.append(
                    f"latest_attendance {latest_att} 比 dump 日 {dump_date} 落差 > 2 天"
                )
        except Exception:
            pass
    if warns:
        return "WARN", warns
    return "PASS", []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dump-date", required=True)
    p.add_argument("--start-ts", type=int, required=True)
    p.add_argument("--download-end-ts", type=int, required=True)
    p.add_argument("--restore-end-ts", type=int, required=True)
    p.add_argument("--sanity-output", required=True)
    args = p.parse_args()

    download_sec = args.download_end_ts - args.start_ts
    restore_sec = args.restore_end_ts - args.download_end_ts
    total_sec = args.restore_end_ts - args.start_ts

    with open(args.sanity_output) as f:
        sanity = f.read()
    parsed = parse_sanity(sanity)
    judgment, warns = judge_pass(parsed, args.dump_date)

    print(f"# DR Restore Drill Report")
    print()
    print(f"- **Dump date:** {args.dump_date}")
    print(f"- **Drill ran at:** {datetime.now(timezone.utc).isoformat()}")
    print(f"- **Judgment:** **{judgment}**")
    if warns:
        print(f"- **Warnings:**")
        for w in warns:
            print(f"  - {w}")
    print()
    print(f"## RTO 拆解（單位：秒）")
    print()
    print(f"| 階段 | 耗時 |")
    print(f"|---|---|")
    print(f"| Download from R2 | {download_sec} |")
    print(f"| pg_restore       | {restore_sec} |")
    print(f"| **Total**        | **{total_sec}** |")
    print()
    print(f"## Sanity SQL 結果")
    print()
    print(f"### Row counts")
    for tbl, n in parsed["row_counts"].items():
        print(f"- `{tbl}`: {n}")
    print()
    print(f"### Latest events")
    for k, v in parsed["latest"].items():
        print(f"- `{k}`: {v}")
    print()
    print(f"### Alembic version")
    print(f"- `{parsed['alembic']}`")
    print()
    print(f"## 原始 psql 輸出")
    print()
    print("```")
    print(sanity)
    print("```")


if __name__ == "__main__":
    main()
