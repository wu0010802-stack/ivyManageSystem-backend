"""year_end package — 年終獎金 6-step 計算引擎 + Excel I/O。"""

from .engine import (
    DeductionBreakdown,
    PerformanceRates,
    SettlementComputed,
    compute_avg_performance_rate,
    compute_deduction_total,
    compute_gross_amount,
    compute_payable_amount,
    compute_proration_rate,
    compute_settlement,
    compute_subtotal_amount,
    compute_total_amount,
)
from .excel_io import (
    ParsedClassEnrollmentTarget,
    ParsedSettlementRow,
    ParsedSpecialBonus,
    ParsedYearEndExcel,
    SummaryExportRow,
    TransferRow,
    YearEndImportResult,
    export_year_end_summary_xlsx,
    export_year_end_transfer_xlsx,
    import_year_end_to_db,
    parse_year_end_excel,
)

__all__ = [
    # engine
    "DeductionBreakdown",
    "PerformanceRates",
    "SettlementComputed",
    "compute_avg_performance_rate",
    "compute_deduction_total",
    "compute_gross_amount",
    "compute_payable_amount",
    "compute_proration_rate",
    "compute_settlement",
    "compute_subtotal_amount",
    "compute_total_amount",
    # excel_io
    "ParsedClassEnrollmentTarget",
    "ParsedSettlementRow",
    "ParsedSpecialBonus",
    "ParsedYearEndExcel",
    "SummaryExportRow",
    "TransferRow",
    "YearEndImportResult",
    "export_year_end_summary_xlsx",
    "export_year_end_transfer_xlsx",
    "import_year_end_to_db",
    "parse_year_end_excel",
]
