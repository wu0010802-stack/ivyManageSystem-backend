"""公開報名端 per-IP 限流改為 env 可調 + NAT 友善預設的回歸測試。

背景：上線穩定度稽核（2026-06-23）發現 register 5/min/IP 在校園/社區/CGNAT
共用同一出口公網 IP 時，多位家長會互相擠掉額度 → 報名尖峰大面積 429（P2）。
改為自 settings.network 讀取（env 可調），並放寬預設。

此測試純讀 config / limiter 物件屬性，不碰 DB（不需 test_db_session fixture）。
"""

from config.network import NetworkSettings


def test_declared_defaults_are_nat_friendly():
    """宣告的預設值放寬到 NAT 友善區間，防止日後被靜默改回原本過緊的值。"""
    f = NetworkSettings.model_fields
    assert f["activity_register_rate_max"].default == 20  # 原 5
    assert f["activity_register_rate_window"].default == 60
    assert f["activity_query_rate_max"].default == 30  # 原 10
    assert f["activity_query_rate_window"].default == 60
    assert f["activity_inquiry_rate_max"].default == 10  # 原 3
    assert f["activity_inquiry_rate_window"].default == 60


def test_public_limiters_wired_to_settings():
    """public.py 三個限流器確實接到 settings.network 值（無硬編碼漂移）。"""
    from config import settings
    from api.activity import public

    assert (
        public._public_register_limiter_instance.max_calls
        == settings.network.activity_register_rate_max
    )
    assert (
        public._public_register_limiter_instance.window
        == settings.network.activity_register_rate_window
    )
    assert (
        public._public_query_limiter_instance.max_calls
        == settings.network.activity_query_rate_max
    )
    assert (
        public._public_query_limiter_instance.window
        == settings.network.activity_query_rate_window
    )
    assert (
        public._public_inquiry_limiter_instance.max_calls
        == settings.network.activity_inquiry_rate_max
    )
    assert (
        public._public_inquiry_limiter_instance.window
        == settings.network.activity_inquiry_rate_window
    )


def test_register_rate_env_override(monkeypatch):
    """可經 env「免改碼」調整限額（上線尖峰調參、報名視窗部署凍結下的逃生口）。"""
    monkeypatch.setenv("ACTIVITY_REGISTER_RATE_MAX", "7")
    monkeypatch.setenv("ACTIVITY_REGISTER_RATE_WINDOW", "30")
    ns = NetworkSettings()
    assert ns.activity_register_rate_max == 7
    assert ns.activity_register_rate_window == 30
