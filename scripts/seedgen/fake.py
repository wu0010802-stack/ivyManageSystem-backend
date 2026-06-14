"""決定論假資料產生器。

`Faker` 持有注入的 `random.Random` 實例,所有亂數一律走它,
**不碰全域 `random`**,以確保同 seed 可重現、跨模組互不污染。
姓名常數複製自 `scripts/seed_test_data_114_2.py`(值,非 import)。
"""

from __future__ import annotations

from datetime import date, timedelta
from random import Random

# 常見姓氏(複製自 seed_test_data_114_2.py 的 SURNAMES 值)。
SURNAMES: list[str] = list(
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪邱曾廖賴徐周葉蘇莊呂江何蕭羅高潘簡朱鍾彭"
    "游詹胡施沈余趙盧梁顏柯孫魏翁戴范方宋鄧杜傅侯曹溫薛丁馬唐卓藍馮姚石董尤巫姜湯汪倪"
)

# 男性名字池(複製自 GIVEN_NAMES_BOY)。
GIVEN_NAMES_MALE: list[str] = [
    "承翰",
    "宥廷",
    "宸睿",
    "品翔",
    "睿恩",
    "宇軒",
    "柏宏",
    "彥廷",
    "辰希",
    "凱翔",
    "立翔",
    "祥宇",
    "致軒",
    "禹辰",
    "亦嘉",
    "佑恩",
    "晨曦",
    "晉彥",
    "皓軒",
    "彥謙",
    "睿廷",
    "信宏",
    "亮廷",
    "韋翔",
    "崇瀚",
]

# 女性名字池(複製自 GIVEN_NAMES_GIRL)。
GIVEN_NAMES_FEMALE: list[str] = [
    "子瑄",
    "宥恩",
    "雅婷",
    "若曦",
    "妤蓁",
    "羽彤",
    "柔安",
    "詠晴",
    "亦晴",
    "于柔",
    "佩瑩",
    "宥彤",
    "祐熙",
    "彤恩",
    "苡晴",
    "婉柔",
    "婕安",
    "歆妍",
    "睿涵",
    "嘉恩",
    "禹彤",
    "若芸",
    "翊婷",
    "巧彤",
    "宥茵",
    "穎萱",
]

# 視為男性的 gender 標籤(其餘一律視為女性)。
_MALE_TOKENS = {"M", "m", "男", "male", "MALE"}

# 身分證首碼字母(地區碼,A~Z)。
_ID_PREFIX_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 地址用的路名片段。
_DISTRICTS = ["中正", "信義", "大安", "中山", "松山", "內湖", "文山", "北投"]
_ROADS = ["和平", "復興", "民生", "民權", "忠孝", "仁愛", "信義", "光復"]


class Faker:
    """決定論假資料產生器,所有方法走注入的 `rng`。"""

    def __init__(self, rng: Random) -> None:
        self.rng = rng

    def _is_male(self, gender: str | None) -> bool:
        return gender in _MALE_TOKENS

    def name(self, gender: str | None = "M") -> str:
        """產生 2~3 字中文姓名(單姓 + 雙字名,共 3 字;名字池亦含,長度 2~3)。"""
        surname = self.rng.choice(SURNAMES)
        pool = GIVEN_NAMES_MALE if self._is_male(gender) else GIVEN_NAMES_FEMALE
        given = self.rng.choice(pool)
        return surname + given

    def phone(self) -> str:
        """台灣手機格式 09 + 8 位數字。"""
        return "09" + "".join(str(self.rng.randint(0, 9)) for _ in range(8))

    def id_number(self, gender: str | None = "M") -> str:
        """台灣身分證格式:大寫字母 + 性別碼(男 1 / 女 2) + 8 位數字。

        注意:此處只保證格式(`^[A-Z][12]\\d{8}$`),不保證檢核碼正確,
        測試資料用途已足夠。
        """
        letter = self.rng.choice(_ID_PREFIX_LETTERS)
        sex_digit = "1" if self._is_male(gender) else "2"
        tail = "".join(str(self.rng.randint(0, 9)) for _ in range(8))
        return f"{letter}{sex_digit}{tail}"

    def address(self) -> str:
        """產生台灣風格地址字串。"""
        district = self.rng.choice(_DISTRICTS)
        road = self.rng.choice(_ROADS)
        section = self.rng.randint(1, 5)
        number = self.rng.randint(1, 300)
        return f"台北市{district}區{road}路{section}段{number}號"

    def birthday(
        self,
        min_age: int,
        max_age: int,
        ref: date | None = None,
    ) -> date:
        """在 [min_age, max_age] 歲區間內回傳一個決定論生日。

        Args:
            min_age: 最小年齡(含)。
            max_age: 最大年齡(含)。
            ref: 參考「今天」,預設 date(2026, 2, 16)。
        """
        if ref is None:
            ref = date(2026, 2, 16)
        # 用天數區間取點,避免閏年/月底邊界問題。
        max_days = max_age * 365 + 90
        min_days = min_age * 365
        offset = self.rng.randint(min_days, max_days)
        return ref - timedelta(days=offset)

    def amount(self, low: int, high: int, step: int = 1) -> int:
        """在 [low, high] 區間內回傳 step 對齊的整數金額。"""
        if step <= 1:
            return self.rng.randint(low, high)
        n_steps = (high - low) // step
        return low + self.rng.randint(0, n_steps) * step
