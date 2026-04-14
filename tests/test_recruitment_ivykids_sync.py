"""義華校官網同步 parser 回歸測試。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import recruitment_ivykids_sync as sync_service


def test_parse_backend_list_row_supports_leading_sort_column():
    row_html = """
    <tr>
        <td></td>
        <td style="color:green">預約正常</td>
        <td>2026-04-15 上午場 10:00</td>
        <td>范瑀玹</td>
        <td>2024-02-20</td>
        <td>0919766932</td>
        <td>親友介紹</td>
        <td>2026-04-12 19:59:45</td>
        <td>
            <a href="form.php?id=177"><button type="button">編輯</button></a>
            <a href="?delid=177"><button type="button">刪除</button></a>
        </td>
    </tr>
    """

    record = sync_service._parse_backend_list_row(
        row_html,
        "https://www.ivykids.tw/manage/make_an_appointment/index.php?page=1",
    )

    assert record is not None
    assert record.external_id == "177"
    assert record.status == "預約正常"
    assert record.visit_date == "2026-04-15 上午場 10:00"
    assert record.child_name == "范瑀玹"
    assert record.phone == "0919766932"
    assert record.source == "親友介紹"
    assert record.created_at == "2026-04-12 19:59:45"
    assert record.month == "115.04"
