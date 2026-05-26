"""ChannelHub() 構造應發出 DeprecationWarning（PR2 後移除）。"""

import warnings


def test_channel_hub_emits_deprecation_warning():
    from utils.ws_hub import ChannelHub

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ChannelHub()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any(
            "ChannelHub" in str(w.message) and "BroadcastBackend" in str(w.message)
            for w in deprecations
        ), f"expected ChannelHub DeprecationWarning, got: {caught}"


def test_channel_hub_still_works():
    """deprecate marker 不該破壞 ChannelHub 既有行為（rebase 保險）。"""
    from utils.ws_hub import ChannelHub

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        hub = ChannelHub()
        # 基本 API 依然可用
        assert hub.channel_size("nobody") == 0
