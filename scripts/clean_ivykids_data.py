"""
一次性清理腳本：刪除從義華官網後台同步進來的所有招生資料。

執行方式（在 backend/ 目錄下）：
    python -m scripts.clean_ivykids_data
"""

import sys
import os

# 確保能 import 專案模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import get_engine, init_database
from models.base import session_scope
from models.recruitment import RecruitmentIvykidsRecord, RecruitmentSyncState

SOURCE = "ivykids_yihua_backend"


def main() -> None:
    init_database()

    with session_scope() as session:
        visit_count = (
            session.query(RecruitmentIvykidsRecord)
            .count()
        )
        state_count = (
            session.query(RecruitmentSyncState)
            .filter(RecruitmentSyncState.provider_name == SOURCE)
            .count()
        )

        if visit_count == 0 and state_count == 0:
            print("資料庫中沒有義華官網後台的同步資料，無需清理。")
            return

        confirm = input(
            f"即將刪除 {visit_count} 筆官網報名記錄"
            f" 及 {state_count} 筆同步狀態記錄，確認請輸入 yes："
        )
        if confirm.strip().lower() != "yes":
            print("已取消。")
            return

        deleted_visits = (
            session.query(RecruitmentIvykidsRecord)
            .delete(synchronize_session=False)
        )
        deleted_states = (
            session.query(RecruitmentSyncState)
            .filter(RecruitmentSyncState.provider_name == SOURCE)
            .delete(synchronize_session=False)
        )

    print(f"已刪除 {deleted_visits} 筆招生訪視記錄。")
    print(f"已刪除 {deleted_states} 筆同步狀態記錄。")
    print("清理完成。")


if __name__ == "__main__":
    main()
