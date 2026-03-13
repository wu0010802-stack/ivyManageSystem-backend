"""執行部署/維運任務：schema migration 與權限回填。"""

import logging
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from main import run_maintenance_tasks

    logger.info("開始執行 maintenance tasks")
    run_maintenance_tasks()
    logger.info("maintenance tasks 完成")


if __name__ == "__main__":
    main()
