"""
勞健保計算服務 - 2026年(民國115年)台灣勞健保級距表
依據政府公布之投保薪資分級表，使用預先計算的保費金額
勞保費率: 12.5% (普通事故11.5% + 就業保險1%)
健保費率: 5.17%
勞退提撥: 雇主6%
"""

from dataclasses import dataclass


@dataclass
class InsuranceCalculation:
    """勞健保計算結果"""

    insured_amount: float
    salary_range: str
    labor_employee: float
    labor_employer: float
    labor_government: float
    health_employee: float
    health_employer: float
    pension_employer: float
    pension_employee: float
    total_employee: float
    total_employer: float


# 2026年(115年)費率設定
LABOR_INSURANCE_RATE = 0.125  # 勞保總費率 12.5%
LABOR_EMPLOYEE_RATIO = 0.20  # 員工負擔 20%
LABOR_EMPLOYER_RATIO = 0.70  # 雇主負擔 70%
LABOR_GOVERNMENT_RATIO = 0.10  # 政府負擔 10%
HEALTH_INSURANCE_RATE = 0.0517  # 健保費率 5.17%
HEALTH_EMPLOYEE_RATIO = 0.30  # 員工負擔 30%
HEALTH_EMPLOYER_RATIO = 0.60  # 雇主負擔 60%
PENSION_EMPLOYER_RATE = 0.06  # 勞退雇主提撥率 6%
AVERAGE_DEPENDENTS = 0.56  # 平均眷屬人數

# 2026年(115年1月1日起適用) 勞保/健保/勞退 三合一級距對照表
# 資料來源: 勞動部勞工保險局、衛生福利部中央健康保險署
# 每筆含: 投保金額, 勞保(員工/雇主), 健保(員工本人/雇主), 勞退雇主提撥
INSURANCE_TABLE_2026 = [
    {
        "amount": 1500,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 90,
    },
    {
        "amount": 3000,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 180,
    },
    {
        "amount": 4500,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 270,
    },
    {
        "amount": 6000,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 360,
    },
    {
        "amount": 7500,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 450,
    },
    {
        "amount": 8700,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 522,
    },
    {
        "amount": 9900,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 594,
    },
    {
        "amount": 11100,
        "labor_employee": 277,
        "labor_employer": 972,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 666,
    },
    {
        "amount": 12540,
        "labor_employee": 313,
        "labor_employer": 1097,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 752,
    },
    {
        "amount": 13500,
        "labor_employee": 338,
        "labor_employer": 1182,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 810,
    },
    {
        "amount": 15840,
        "labor_employee": 396,
        "labor_employer": 1386,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 950,
    },
    {
        "amount": 16500,
        "labor_employee": 413,
        "labor_employer": 1444,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 990,
    },
    {
        "amount": 17280,
        "labor_employee": 432,
        "labor_employer": 1512,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1037,
    },
    {
        "amount": 17880,
        "labor_employee": 447,
        "labor_employer": 1564,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1073,
    },
    {
        "amount": 19047,
        "labor_employee": 476,
        "labor_employer": 1666,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1143,
    },
    {
        "amount": 20008,
        "labor_employee": 500,
        "labor_employer": 1751,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1200,
    },
    {
        "amount": 21009,
        "labor_employee": 525,
        "labor_employer": 1838,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1261,
    },
    {
        "amount": 22000,
        "labor_employee": 550,
        "labor_employer": 1925,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1320,
    },
    {
        "amount": 23100,
        "labor_employee": 577,
        "labor_employer": 2022,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1386,
    },
    {
        "amount": 24000,
        "labor_employee": 600,
        "labor_employer": 2100,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1440,
    },
    {
        "amount": 25250,
        "labor_employee": 632,
        "labor_employer": 2210,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1515,
    },
    {
        "amount": 26400,
        "labor_employee": 660,
        "labor_employer": 2310,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1584,
    },
    {
        "amount": 27600,
        "labor_employee": 690,
        "labor_employer": 2415,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1656,
    },
    {
        "amount": 28590,
        "labor_employee": 715,
        "labor_employer": 2501,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1715,
    },
    {
        "amount": 29500,
        "labor_employee": 738,
        "labor_employer": 2582,
        "health_employee": 458,
        "health_employer": 1428,
        "pension": 1770,
    },
    {
        "amount": 30300,
        "labor_employee": 758,
        "labor_employer": 2651,
        "health_employee": 470,
        "health_employer": 1466,
        "pension": 1818,
    },
    {
        "amount": 31800,
        "labor_employee": 795,
        "labor_employer": 2783,
        "health_employee": 493,
        "health_employer": 1539,
        "pension": 1908,
    },
    {
        "amount": 33300,
        "labor_employee": 833,
        "labor_employer": 2914,
        "health_employee": 516,
        "health_employer": 1611,
        "pension": 1998,
    },
    {
        "amount": 34800,
        "labor_employee": 870,
        "labor_employer": 3045,
        "health_employee": 540,
        "health_employer": 1684,
        "pension": 2088,
    },
    {
        "amount": 36300,
        "labor_employee": 908,
        "labor_employer": 3176,
        "health_employee": 563,
        "health_employer": 1757,
        "pension": 2178,
    },
    {
        "amount": 38200,
        "labor_employee": 955,
        "labor_employer": 3342,
        "health_employee": 592,
        "health_employer": 1849,
        "pension": 2292,
    },
    {
        "amount": 40100,
        "labor_employee": 1002,
        "labor_employer": 3509,
        "health_employee": 622,
        "health_employer": 1940,
        "pension": 2406,
    },
    {
        "amount": 42000,
        "labor_employee": 1050,
        "labor_employer": 3675,
        "health_employee": 651,
        "health_employer": 2032,
        "pension": 2520,
    },
    {
        "amount": 43900,
        "labor_employee": 1098,
        "labor_employer": 3841,
        "health_employee": 681,
        "health_employer": 2124,
        "pension": 2634,
    },
    {
        "amount": 45800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 710,
        "health_employer": 2216,
        "pension": 2748,
    },
    {
        "amount": 48200,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 748,
        "health_employer": 2332,
        "pension": 2892,
    },
    {
        "amount": 50600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 785,
        "health_employer": 2449,
        "pension": 3036,
    },
    {
        "amount": 53000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 822,
        "health_employer": 2565,
        "pension": 3180,
    },
    {
        "amount": 55400,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 859,
        "health_employer": 2681,
        "pension": 3324,
    },
    {
        "amount": 57800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 896,
        "health_employer": 2797,
        "pension": 3468,
    },
    {
        "amount": 60800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 943,
        "health_employer": 2942,
        "pension": 3648,
    },
    {
        "amount": 63800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 990,
        "health_employer": 3087,
        "pension": 3828,
    },
    {
        "amount": 66800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1036,
        "health_employer": 3233,
        "pension": 4008,
    },
    {
        "amount": 69800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1083,
        "health_employer": 3378,
        "pension": 4188,
    },
    {
        "amount": 72800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1129,
        "health_employer": 3523,
        "pension": 4368,
    },
    {
        "amount": 76500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1187,
        "health_employer": 3702,
        "pension": 4590,
    },
    {
        "amount": 80200,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1244,
        "health_employer": 3881,
        "pension": 4812,
    },
    {
        "amount": 83900,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1301,
        "health_employer": 4060,
        "pension": 5034,
    },
    {
        "amount": 87600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1359,
        "health_employer": 4239,
        "pension": 5256,
    },
    {
        "amount": 92100,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1428,
        "health_employer": 4457,
        "pension": 5526,
    },
    {
        "amount": 96600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1498,
        "health_employer": 4675,
        "pension": 5796,
    },
    {
        "amount": 101100,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1568,
        "health_employer": 4892,
        "pension": 6066,
    },
    {
        "amount": 105600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1638,
        "health_employer": 5110,
        "pension": 6336,
    },
    {
        "amount": 110100,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1708,
        "health_employer": 5328,
        "pension": 6606,
    },
    {
        "amount": 115500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1791,
        "health_employer": 5589,
        "pension": 6930,
    },
    {
        "amount": 120900,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1875,
        "health_employer": 5850,
        "pension": 7254,
    },
    {
        "amount": 126300,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 1959,
        "health_employer": 6112,
        "pension": 7578,
    },
    {
        "amount": 131700,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2043,
        "health_employer": 6373,
        "pension": 7902,
    },
    {
        "amount": 137100,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2126,
        "health_employer": 6634,
        "pension": 8226,
    },
    {
        "amount": 142500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2210,
        "health_employer": 6896,
        "pension": 8550,
    },
    {
        "amount": 147900,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2294,
        "health_employer": 7157,
        "pension": 8874,
    },
    {
        "amount": 150000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2327,
        "health_employer": 7259,
        "pension": 9000,
    },
    {
        "amount": 156400,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2426,
        "health_employer": 7568,
        "pension": 9000,
    },
    {
        "amount": 162800,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2525,
        "health_employer": 7878,
        "pension": 9000,
    },
    {
        "amount": 169200,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2624,
        "health_employer": 8188,
        "pension": 9000,
    },
    {
        "amount": 175600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2724,
        "health_employer": 8497,
        "pension": 9000,
    },
    {
        "amount": 182000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2823,
        "health_employer": 8807,
        "pension": 9000,
    },
    {
        "amount": 189500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 2939,
        "health_employer": 9170,
        "pension": 9000,
    },
    {
        "amount": 197000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3055,
        "health_employer": 9533,
        "pension": 9000,
    },
    {
        "amount": 204500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3172,
        "health_employer": 9896,
        "pension": 9000,
    },
    {
        "amount": 212000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3288,
        "health_employer": 10259,
        "pension": 9000,
    },
    {
        "amount": 219500,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3404,
        "health_employer": 10622,
        "pension": 9000,
    },
    {
        "amount": 228200,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3539,
        "health_employer": 11043,
        "pension": 9000,
    },
    {
        "amount": 236900,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3674,
        "health_employer": 11464,
        "pension": 9000,
    },
    {
        "amount": 245600,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3809,
        "health_employer": 11885,
        "pension": 9000,
    },
    {
        "amount": 254300,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 3944,
        "health_employer": 12306,
        "pension": 9000,
    },
    {
        "amount": 263000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4079,
        "health_employer": 12727,
        "pension": 9000,
    },
    {
        "amount": 273000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4234,
        "health_employer": 13211,
        "pension": 9000,
    },
    {
        "amount": 283000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4389,
        "health_employer": 13695,
        "pension": 9000,
    },
    {
        "amount": 293000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4544,
        "health_employer": 14179,
        "pension": 9000,
    },
    {
        "amount": 303000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4700,
        "health_employer": 14663,
        "pension": 9000,
    },
    {
        "amount": 313000,
        "labor_employee": 1145,
        "labor_employer": 4008,
        "health_employee": 4855,
        "health_employer": 15146,
        "pension": 9000,
    },
]


class InsuranceService:
    def __init__(self):
        self.table = INSURANCE_TABLE_2026

    def get_bracket(self, salary: float) -> dict:
        """根據薪資查找對應的級距（薪資介於兩個級距之間取較高級數）"""
        for entry in self.table:
            if salary <= entry["amount"]:
                return entry
        return self.table[-1]

    def calculate(
        self, salary: float, dependents: int = 0, pension_self_rate: float = 0
    ) -> InsuranceCalculation:
        """計算勞健保及勞退費用"""
        if salary < 0:
            raise ValueError(f"投保薪資不可為負數：{salary}")
        if not 0 <= pension_self_rate <= 0.06:
            raise ValueError(f"勞退自提比例必須介於 0～6%：{pension_self_rate}")
        bracket = self.get_bracket(salary)
        amount = bracket["amount"]

        labor_emp = bracket["labor_employee"]
        labor_er = bracket["labor_employer"]
        labor_gov = round(amount * LABOR_INSURANCE_RATE * LABOR_GOVERNMENT_RATIO)

        # 健保員工自付額依眷屬人數倍增（最多3人；負值以0計，防止DB舊資料或直接寫入產生負健保費）
        health_emp_base = bracket["health_employee"]
        health_emp = health_emp_base * (1 + min(max(0, dependents), 3))
        health_er = bracket["health_employer"]

        pension_er = bracket["pension"]
        pension_emp = round(
            bracket["amount"] * pension_self_rate
        )  # 依勞基法以月提繳工資級距計算

        return InsuranceCalculation(
            insured_amount=amount,
            salary_range=f"{amount:,}",
            labor_employee=labor_emp,
            labor_employer=labor_er,
            labor_government=labor_gov,
            health_employee=health_emp,
            health_employer=health_er,
            pension_employer=pension_er,
            pension_employee=pension_emp,
            total_employee=labor_emp + health_emp + pension_emp,
            total_employer=labor_er + health_er + pension_er,
        )
