"""seedgen CLI 入口(`python -m scripts.seedgen`)。

流程:
  解析參數 → 建 SeedConfig → 取 settings.core.{database_url, env}
  → guard.assert_dev_db 把關 → session_scope() 開 session → 建 SeedContext
  → --wipe(且 --yes)則 wipe,否則 dry-run 印將清表清單
  → 依執行序 import 並跑 m00..m14 的 seed(ctx)(--only 可過濾),每模組跑完 commit
  → 末了 verify.summary 印各表筆數。

「全 stub」狀態下(各模組 seed 為 pass)應乾淨跑完。
"""

from __future__ import annotations

import argparse
import logging
import random
from datetime import date

from . import guard, verify, wipe
from .config import SeedConfig
from .context import SeedContext

logger = logging.getLogger(__name__)

# orchestrator 執行序:模組名(對應 modules/<name>.py)。
_MODULE_ORDER: tuple[str, ...] = (
    "m00_config",
    "m01_org",
    "m02_students",
    "m03_attendance",
    "m04_leave_ot",
    "m05_fees",
    "m06_salary",
    "m07_activities",
    "m08_portal",
    "m09_parent",
    "m10_medical",
    "m11_special_ed",
    "m12_appraisal",
    "m13_year_end",
    "m14_audit_misc",
)


def _build_parser() -> argparse.ArgumentParser:
    """組裝 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seedgen",
        description="可參數化全年測試資料產生器(僅限本機 dev DB)。",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="民國學年。預設由 --today 所屬學年自動推導(real today 多落 114/115)。",
    )
    parser.add_argument(
        "--today",
        type=str,
        default=None,
        help=(
            "模擬「今天」(ISO 日期),決定封存/進行中/未來月份。"
            "預設=真實當天,使 app 的『當前學期/今日』視圖與 seed 資料對齊。"
        ),
    )
    parser.add_argument(
        "--scale",
        type=str,
        default="standard",
        choices=["standard", "small", "large"],
        help="規模 profile(預設 standard)。",
    )
    parser.add_argument(
        "--rng-seed",
        type=int,
        default=20260614,
        help="決定論隨機種子(預設 20260614)。",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="清除既有業務資料(需搭配 --yes 才真正執行)。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="確認執行破壞性操作(--wipe)。未帶則只 dry-run 印計畫。",
    )
    parser.add_argument(
        "--i-know-not-dev",
        action="store_true",
        help="略過 dev DB 護欄(危險,僅在明知非 dev 時使用)。",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="只跑指定模組,逗號分隔(如 m00,m01)。空則全跑。",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="只驗證現有 DB(summary + 一致性檢查),不 wipe、不 seed。",
    )
    return parser


def _parse_only(raw: str) -> tuple[str, ...]:
    """把 --only 的逗號字串轉成模組前綴 tuple(去空白/空項)。"""
    return tuple(token.strip() for token in raw.split(",") if token.strip())


def _selected_modules(only: tuple[str, ...]) -> list[str]:
    """依 --only 前綴(如 m00)過濾 _MODULE_ORDER,保持執行序。"""
    if not only:
        return list(_MODULE_ORDER)
    selected: list[str] = []
    for module_name in _MODULE_ORDER:
        prefix = module_name.split("_", 1)[0]  # m00_config → m00
        if prefix in only or module_name in only:
            selected.append(module_name)
    return selected


def _config_from_args(args: argparse.Namespace) -> SeedConfig:
    """把解析後參數組成 frozen SeedConfig。

    --today 預設=真實當天(讓 app 的『今日/當前學期』視圖與 seed 資料對齊);
    --year 預設=該 today 所屬民國學年(由 current_term 推導),避免 today 與
    academic_year 跨學年不一致導致班級/課程被當前學期過濾濾掉。
    """
    from datetime import date as _date

    from .calendar import current_term

    today = _date.fromisoformat(args.today) if args.today else _date.today()
    year = args.year if args.year is not None else current_term(today)[0]
    return SeedConfig(
        academic_year=year,
        today=today,
        scale=args.scale,
        rng_seed=args.rng_seed,
        wipe=args.wipe,
        confirm=args.yes,
        allow_non_dev=args.i_know_not_dev,
        only=_parse_only(args.only),
    )


def _run_modules(ctx: SeedContext, module_names: list[str]) -> None:
    """依序 import 並執行各模組 seed(ctx),每模組跑完 commit。"""
    import importlib

    for module_name in module_names:
        module = importlib.import_module(f"scripts.seedgen.modules.{module_name}")
        logger.info("執行模組 %s.seed(ctx)", module_name)
        module.seed(ctx)
        if ctx.session is not None:
            ctx.session.commit()


def main(argv: list[str] | None = None) -> int:
    """CLI 主流程,回傳 process exit code。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    cfg = _config_from_args(args)

    # 延遲 import settings/models:讓 --help 不觸發設定/DB 連線。
    from config import settings

    # 護欄必須驗證引擎「實際」連線的 URL。settings.core.database_url 在
    # DATABASE_URL 未設時為 None(dev 常態),而真正連線字串由 models.base 在
    # dev 下 materialise 成 localhost 預設值(postgresql://localhost:5432/
    # ivymanagement)。直接取 models.base.DATABASE_URL 可避免護欄與 session_scope
    # 連線目標分歧(否則 None 會被誤判為遠端而擋掉合法的本機 dry-run)。
    from models.base import DATABASE_URL as engine_database_url

    env = settings.core.env
    guard.assert_dev_db(engine_database_url, env, cfg.allow_non_dev)

    module_names = _selected_modules(cfg.only)

    from models.base import session_scope

    with session_scope() as session:
        ctx = SeedContext(
            session=session,
            config=cfg,
            rng=random.Random(cfg.rng_seed),
        )

        # --verify:只驗證現有 DB,不 wipe 不 seed。
        if args.verify:
            verify.summary(session)
            problems = verify.check_consistency(session)
            if problems:
                print("=== ✗ 一致性問題 ===")
                for p in problems:
                    print(f"  ✗ {p}")
            else:
                print("=== ✓ 一致性檢查全數通過 ===")
            return 1 if problems else 0

        targets = wipe.tables_to_wipe()
        # 本工具只支援「wipe 後重灌」(模組假設全新空表,否則 enrollment_seq 等
        # 唯一鍵會碰撞);故 seed 僅在 --wipe --yes 同時成立時執行。
        # 例外:指定 --only 時(debug 單模組補跑)允許不 wipe 直接 seed 選定模組,
        # 供整合除錯(模組僅在 seed() 結束才 commit,中途失敗 rollback,重跑安全)。
        do_seed = (cfg.wipe and cfg.confirm) or bool(cfg.only)

        if cfg.wipe:
            print(f"[wipe] 將清除 {len(targets)} 張表:")
            for name in targets:
                print(f"  - {name}")
            if cfg.confirm:
                wipe.wipe(session)
                session.commit()
                print("[wipe] 已清除並重置序列。")
            else:
                print("[wipe] dry-run(未帶 --yes):不清除、不 seed。")
        else:
            print(
                f"[plan-only] 未帶 --wipe;若清除將涉及 {len(targets)} 張表。"
                "不 seed(本工具僅支援 wipe 後重灌,請用 --wipe --yes)。"
            )

        if do_seed:
            print(f"[run] 依序執行模組:{', '.join(module_names)}")
            _run_modules(ctx, module_names)
            verify.summary(session)
        else:
            print("[plan-only] 未執行 seed(需 --wipe --yes)。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
