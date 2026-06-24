#!/usr/bin/env python3
"""Alembic migration 快速靜態 gate（無需 DB）——防 cold-start migration 失敗造成 boot loop。

背景（崩潰防護 P0）：prod push = Zeabur cold-start 跑 `alembic upgrade heads`（見
startup/migrations.run_alembic_upgrade）。一支壞 migration 會讓啟動失敗 → 反覆重啟
（全服務 down）。本 gate 在 CI / push 前以**靜態檢查**攔下最常見的 boot-loop 成因：

1. 單一 head：多 head（常見於平行分支各自 add migration 後 merge 未開 merge migration）
   會讓鏈狀升級語意混亂、回滾困難。`upgrade heads`(複數) 雖會套用全部，但多 head 仍應
   顯式 merge，不該悄悄存在。
2. 全部 version 檔可乾淨 import（語法/import 錯 → alembic 一律無法載入 → 啟動即炸）。
3. 每支 migration 都定義 upgrade() 與 downgrade()（缺 downgrade → 無法回滾）。

⚠ 本 gate 「不」做 from-empty 的 upgrade 預演：baseline migration 假設 schema 已存在，
且 prod 以 create_all+stamp 建立（跳過 op.execute 基礎建設，見記憶
reference_prod_create_all_stamp_skips_infra）→ 從 create_all 基底跑 downgrade/upgrade
會因缺 SECURITY DEFINER function / role 等而**誤失敗**（2026-06-24 實測確認）。
真正「會不會在 prod 上套用成功」的權威預演 = 對 **DR 還原的 prod 副本** 跑 cold-start
路徑（見 docs/sop/zeabur-deployment-runbook.md）。
"""

from __future__ import annotations

import sys
from pathlib import Path


def check_single_head(heads: list[str]) -> list[str]:
    """回傳問題清單（空 = 通過）。多 head → 報錯。"""
    problems: list[str] = []
    if len(heads) == 0:
        problems.append("找不到任何 alembic head（migration 鏈異常）")
    elif len(heads) > 1:
        problems.append(
            "偵測到多個 alembic head："
            + ", ".join(sorted(heads))
            + "——平行分支各自新增 migration，需新增 merge migration 收斂成單一 head，"
            "否則回滾語意混亂。"
        )
    return problems


def check_upgrade_downgrade_present(revisions) -> list[str]:
    """每支 migration 都須有 upgrade() 與 downgrade()。"""
    problems: list[str] = []
    for rev in revisions:
        mod = rev.module
        if not callable(getattr(mod, "upgrade", None)):
            problems.append(f"{rev.revision} 缺 upgrade()")
        if not callable(getattr(mod, "downgrade", None)):
            problems.append(
                f"{rev.revision} 缺 downgrade()（無法回滾，部署出錯時無退路）"
            )
    return problems


def main() -> int:
    # 延後 import alembic，讓純函式（上方）可在無 alembic 的環境被單元測試。
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))

    # from_config + walk_revisions 會載入全部 version 檔 → 語法/import 錯在此 surface。
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())

    problems: list[str] = []
    problems += check_single_head(list(script.get_heads()))
    problems += check_upgrade_downgrade_present(revisions)

    if problems:
        print("❌ migration 靜態 gate 失敗：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"✓ migration 靜態 gate 通過（{len(revisions)} 支 migration，單一 head）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
