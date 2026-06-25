"""Scale-out 協調 gate（設計審查 2026-06-25 LONG-1，主題：可擴展性拓樸）。

問題：本服務多處用 in-process memory backend（cache / WS broadcast / rate-limit），
假設【單 uvicorn worker】部署。要 scale-out 到多 worker / 多 pod，必須同時把這幾個
backend 切成共享後端（Redis / Postgres），否則：
  - CACHE_BACKEND=memory → 各 worker 快取分裂（讀到彼此不一致）
  - BROADCAST_BACKEND=memory → WS 廣播無法跨 worker（誤踢/漏推訂閱者）
  - RATE_LIMIT_BACKEND=memory → 每 worker 各算各的 → 限流塌成「桶數 × worker」

原本 main.py 只在 backend=memory 時 WARNING（無論單/多 worker），這個「scale-out 要翻
好幾個 env flag」的隱性合約沒有強制點——操作者只要漏翻一個就靜默分裂。本 gate 把它
收斂成單一 fail-fast 守衛：``DEPLOYMENT_MODE=multi`` 時任一 backend 仍 memory → 拒啟動。

對當前部署（``DEPLOYMENT_MODE`` 未設 = single）**零行為改變**——gate 直接 return。
"""

from __future__ import annotations


class ScaleOutMisconfigError(RuntimeError):
    """DEPLOYMENT_MODE=multi 但跨 worker backend 仍為 in-process memory。"""


def check_scale_out_backends(settings) -> None:
    """multi 模式下強制跨 worker backend 非 memory，否則 raise ScaleOutMisconfigError。

    single（預設 / 當前 prod）→ 直接 return，不檢查、零行為改變。
    """
    if settings.core.deployment_mode != "multi":
        return

    offenders: list[str] = []
    if settings.cache.backend == "memory":
        offenders.append("CACHE_BACKEND=memory（請設 redis 並提供 CACHE_REDIS_URL）")
    if settings.cache.effective_broadcast_backend == "memory":
        offenders.append("BROADCAST_BACKEND=memory（請設 redis）")
    if settings.network.rate_limit_backend.lower() == "memory":
        offenders.append("RATE_LIMIT_BACKEND=memory（請設 postgres 或 redis）")

    if offenders:
        raise ScaleOutMisconfigError(
            "DEPLOYMENT_MODE=multi 但下列跨 worker backend 仍為 in-process memory，"
            "多 worker / 多 pod 會造成快取分裂 / WS 廣播跨 worker 失效 / 限流塌成單桶——"
            "拒絕啟動以免靜默分裂：" + "；".join(offenders)
        )
