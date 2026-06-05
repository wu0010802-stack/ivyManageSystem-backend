"""scripts/seed/fee_templates.py — 學費範本(FeeTemplate)冪等 seed。

包裝既有 scripts.seed_fee_templates.seed()(4 年級 × 2 學期 × 3 費用類型 = 24 筆)。
直接跑 scripts.seed_fee_templates 會因未載入全 model 而 mapper 解析失敗;
本包裝先經 scripts.seed._common(其 import models.database)載入全 model 再呼叫。
"""

from __future__ import annotations

import logging

from scripts.seed._common import session_scope  # noqa: F401  觸發全 model 載入
from scripts.seed_fee_templates import seed as _seed

logger = logging.getLogger(__name__)


def step() -> None:
    res = _seed()
    logger.info("fee_templates seed: %s", res)
    print(f"[fee_templates] {res}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
