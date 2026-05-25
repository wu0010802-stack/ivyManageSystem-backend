"""inbox_ws skeleton 行為測試。"""

import pytest
from api import inbox_ws


def test_inbox_user_key_returns_tuple():
    assert inbox_ws.INBOX_USER_KEY(42) == ("inbox_user", 42)


@pytest.mark.anyio
async def test_inbox_broadcast_user_no_subscribers_is_no_op():
    """無 WS subscriber 時 broadcast 不應拋例外。"""
    await inbox_ws.inbox_broadcast_user(999, {"event_type": "leave.approved"})
    # 沒拋例外即通過
