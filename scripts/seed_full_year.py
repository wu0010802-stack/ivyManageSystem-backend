"""scripts/seed_full_year.py — 一鍵灌全 114 學年假資料（dev 手測/展示用）。

整合兩部分:
1. 核心營運模組(scripts/seed_test_data_114_2.py):一次性步驟 + 逐學期步驟(上+下學期)。
2. 冷門模組(scripts/seed/*.py):自動探索每個模組的 step() 並執行。

全程冪等:重跑只補缺、不重複、不刪改現有資料。

用法:
    cd ~/Desktop/ivy-backend
    python -m scripts.seed_full_year                # 跑全部(核心 + 冷門)
    python -m scripts.seed_full_year --only core     # 只跑核心營運
    python -m scripts.seed_full_year --only cold      # 只跑冷門模組
    python -m scripts.seed_full_year --term 114_1     # 核心只跑指定學期
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_full_year")


def run_core(terms: list[str]) -> None:
    """核心營運:一次性步驟跑一次 + 逐學期步驟每學期跑一輪。"""
    import scripts.seed_test_data_114_2 as core

    logger.info("===== 核心營運模組 =====")
    for name, fn in core.ONE_TIME_STEPS.items():
        logger.info("[core/one-time] %s", name)
        fn()
    for term in terms:
        core.set_term(term)
        logger.info("----- 學期 %s (%s ~ %s) -----", term, core.TERM_START, core.TODAY)
        for name, fn in core.TERM_STEPS.items():
            logger.info("[core/%s] %s", term, name)
            fn()


def run_cold() -> None:
    """冷門模組:自動探索 scripts/seed/*.py 的 step() 並執行。"""
    import scripts.seed as seed_pkg

    logger.info("===== 冷門模組 =====")
    mod_names = sorted(
        m.name
        for m in pkgutil.iter_modules(seed_pkg.__path__)
        if not m.name.startswith("_")
    )
    for name in mod_names:
        mod = importlib.import_module(f"scripts.seed.{name}")
        step = getattr(mod, "step", None)
        if step is None:
            logger.warning("[cold] %s 無 step()，跳過", name)
            continue
        logger.info("[cold] %s", name)
        try:
            step()
        except Exception:
            logger.exception("[cold] %s 失敗", name)
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["core", "cold"],
        default=None,
        help="只跑 core 或 cold;預設兩者都跑。",
    )
    parser.add_argument(
        "--term",
        default="all",
        help="核心逐學期步驟的學期:all(上+下)、114_1、114_2。",
    )
    args = parser.parse_args()

    terms = ["114_1", "114_2"] if args.term == "all" else [args.term]

    if args.only != "cold":
        run_core(terms)
    if args.only != "core":
        run_cold()
    logger.info("===== 全學年 seed 完成 =====")


if __name__ == "__main__":
    main()
