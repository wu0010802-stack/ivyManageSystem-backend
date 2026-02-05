"""
勞健保計算服務 - 2026年台灣勞健保級距表
"""

import pandas as pd
from typing import Dict, List, Tuple, Optional
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


# 2026年費率設定
LABOR_INSURANCE_RATE = 0.12
LABOR_EMPLOYEE_RATIO = 0.20
LABOR_EMPLOYER_RATIO = 0.70
LABOR_GOVERNMENT_RATIO = 0.10
HEALTH_INSURANCE_RATE = 0.0517
HEALTH_EMPLOYEE_RATIO = 0.30
HEALTH_EMPLOYER_RATIO = 0.60
PENSION_EMPLOYER_RATE = 0.06
AVERAGE_DEPENDENTS = 0.57

# 勞保級距表
LABOR_LEVELS = [
    {"level": 1, "min": 0, "max": 27470, "insured": 27470},
    {"level": 2, "min": 27471, "max": 28800, "insured": 28800},
    {"level": 3, "min": 28801, "max": 30300, "insured": 30300},
    {"level": 4, "min": 30301, "max": 31800, "insured": 31800},
    {"level": 5, "min": 31801, "max": 33300, "insured": 33300},
    {"level": 6, "min": 33301, "max": 34800, "insured": 34800},
    {"level": 7, "min": 34801, "max": 36300, "insured": 36300},
    {"level": 8, "min": 36301, "max": 38200, "insured": 38200},
    {"level": 9, "min": 38201, "max": 40100, "insured": 40100},
    {"level": 10, "min": 40101, "max": 42000, "insured": 42000},
    {"level": 11, "min": 42001, "max": 43900, "insured": 43900},
    {"level": 12, "min": 43901, "max": 45800, "insured": 45800},
    {"level": 13, "min": 45801, "max": 48200, "insured": 48200},
    {"level": 14, "min": 48201, "max": 50600, "insured": 50600},
    {"level": 15, "min": 50601, "max": 53000, "insured": 53000},
    {"level": 16, "min": 53001, "max": float('inf'), "insured": 45800},
]

# 健保級距表
HEALTH_LEVELS = [
    {"min": 0, "max": 27470, "insured": 27470},
    {"min": 27471, "max": 28800, "insured": 28800},
    {"min": 28801, "max": 30300, "insured": 30300},
    {"min": 30301, "max": 31800, "insured": 31800},
    {"min": 31801, "max": 33300, "insured": 33300},
    {"min": 33301, "max": 34800, "insured": 34800},
    {"min": 34801, "max": 36300, "insured": 36300},
    {"min": 36301, "max": 38200, "insured": 38200},
    {"min": 38201, "max": 40100, "insured": 40100},
    {"min": 40101, "max": 42000, "insured": 42000},
    {"min": 42001, "max": 43900, "insured": 43900},
    {"min": 43901, "max": 45800, "insured": 45800},
    {"min": 45801, "max": 48200, "insured": 48200},
    {"min": 48201, "max": 50600, "insured": 50600},
    {"min": 50601, "max": 53000, "insured": 53000},
    {"min": 53001, "max": 60800, "insured": 60800},
    {"min": 60801, "max": 72800, "insured": 72800},
    {"min": 72801, "max": 87600, "insured": 87600},
    {"min": 87601, "max": 110100, "insured": 110100},
    {"min": 110101, "max": 150000, "insured": 150000},
    {"min": 150001, "max": float('inf'), "insured": 189500},
]


class InsuranceService:
    def __init__(self):
        self.custom_labor_levels = None
        self.custom_health_levels = None
    
    def import_table(self, file_path: str = None, data: List[dict] = None, table_type: str = "labor") -> bool:
        """匯入自訂級距表"""
        try:
            if file_path:
                df = pd.read_csv(file_path) if file_path.endswith('.csv') else pd.read_excel(file_path)
                levels = [{"min": row.get('min', 0), "max": row.get('max', float('inf')), "insured": row.get('insured', 0)} for _, row in df.iterrows()]
            else:
                levels = data
            if table_type == "labor":
                self.custom_labor_levels = levels
            else:
                self.custom_health_levels = levels
            return True
        except Exception as e:
            return False
    
    def get_insured_amount(self, salary: float, table_type: str = "labor") -> Tuple[float, str]:
        levels = (self.custom_labor_levels or LABOR_LEVELS) if table_type == "labor" else (self.custom_health_levels or HEALTH_LEVELS)
        for lvl in levels:
            if lvl['min'] <= salary <= lvl['max']:
                return lvl['insured'], f"{lvl['min']:,.0f}~{lvl['max']:,.0f}" if lvl['max'] != float('inf') else f"{lvl['min']:,.0f}+"
        return levels[-1]['insured'], f"{levels[-1]['min']:,.0f}+"
    
    def calculate(self, salary: float, dependents: int = 0, pension_self_rate: float = 0) -> InsuranceCalculation:
        labor_insured, labor_range = self.get_insured_amount(salary, "labor")
        health_insured, _ = self.get_insured_amount(salary, "health")
        
        labor_total = labor_insured * LABOR_INSURANCE_RATE
        labor_emp = round(labor_total * LABOR_EMPLOYEE_RATIO)
        labor_er = round(labor_total * LABOR_EMPLOYER_RATIO)
        labor_gov = round(labor_total * LABOR_GOVERNMENT_RATIO)
        
        health_base = health_insured * HEALTH_INSURANCE_RATE
        health_emp = round(health_base * HEALTH_EMPLOYEE_RATIO * (1 + min(dependents, 3)))
        health_er = round(health_base * HEALTH_EMPLOYER_RATIO * (1 + AVERAGE_DEPENDENTS))
        
        pension_er = round(salary * PENSION_EMPLOYER_RATE)
        pension_emp = round(salary * pension_self_rate)
        
        return InsuranceCalculation(
            insured_amount=labor_insured, salary_range=labor_range,
            labor_employee=labor_emp, labor_employer=labor_er, labor_government=labor_gov,
            health_employee=health_emp, health_employer=health_er,
            pension_employer=pension_er, pension_employee=pension_emp,
            total_employee=labor_emp + health_emp + pension_emp,
            total_employer=labor_er + health_er + pension_er
        )
