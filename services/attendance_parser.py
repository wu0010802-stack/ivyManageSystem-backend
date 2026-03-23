"""
考勤解析引擎 - 解析打卡記錄 Excel 並計算遲到/未打卡次數
"""

import pandas as pd
from datetime import datetime, time, timedelta
from typing import Dict, List, Tuple
from dataclasses import dataclass


@dataclass
class AttendanceResult:
    """考勤分析結果"""
    employee_name: str
    total_days: int
    normal_days: int
    late_count: int
    early_leave_count: int
    missing_punch_in_count: int
    missing_punch_out_count: int
    total_late_minutes: int
    total_early_minutes: int
    details: List[dict]


class AttendanceParser:
    """
    打卡記錄解析器

    解析打卡機 Excel 檔案，自動計算每位員工的：
    - 遲到次數與總分鐘數
    - 早退次數與總分鐘數
    - 未打卡次數（上班/下班）
    """

    # 預設上下班時間
    DEFAULT_WORK_START = "08:00"
    DEFAULT_WORK_END = "17:00"

    # 寬限時間（分鐘）
    LATE_GRACE_MINUTES = 5

    # 跨夜班判斷門檻：
    #   前一日單筆打卡 ≥ OVERNIGHT_START → 可能是夜班開始
    #   當日單筆打卡  < OVERNIGHT_END   → 可能是夜班結束（歸入前一日）
    OVERNIGHT_START = time(16, 0)
    OVERNIGHT_END   = time(6, 0)
    
    def __init__(self, employee_schedules: Dict[str, dict] = None):
        """
        初始化解析器
        
        Args:
            employee_schedules: 員工班表設定 {姓名: {work_start: "08:00", work_end: "17:00"}}
        """
        self.employee_schedules = employee_schedules or {}
    
    def parse_attendance_excel(
        self, 
        file_path: str,
        name_column: str = "姓名",
        datetime_column: str = "時間",
        date_column: str = None,
        time_column: str = None
    ) -> Dict[str, AttendanceResult]:
        """
        解析打卡記錄 Excel
        
        支援兩種格式：
        1. 單一時間欄位：包含完整日期時間
        2. 分開欄位：日期與時間分開
        
        Args:
            file_path: Excel 檔案路徑
            name_column: 姓名欄位名稱
            datetime_column: 時間欄位名稱（包含完整日期時間）
            date_column: 日期欄位名稱（可選）
            time_column: 時間欄位名稱（可選）
        
        Returns:
            Dict[員工姓名, 考勤分析結果]
        """
        # 讀取 Excel
        df = pd.read_excel(file_path)
        
        # 處理時間欄位
        if date_column and time_column:
            # 分開欄位的情況
            df['punch_datetime'] = pd.to_datetime(
                df[date_column].astype(str) + ' ' + df[time_column].astype(str)
            )
        else:
            # 單一時間欄位
            df['punch_datetime'] = pd.to_datetime(df[datetime_column])
        
        df['punch_date'] = df['punch_datetime'].dt.date
        df['punch_time'] = df['punch_datetime'].dt.time
        
        # 按員工分組處理
        results = {}
        
        for employee_name in df[name_column].unique():
            employee_df = df[df[name_column] == employee_name].copy()
            result = self._analyze_employee_attendance(employee_name, employee_df)
            results[employee_name] = result
        
        return results
    
    def _stitch_overnight_punches(
        self, employee_df: pd.DataFrame
    ) -> tuple:
        """
        跨夜班縫合：將隔日清晨的打卡歸入前一個工作日。

        判斷規則：
          1. Day N 最早一筆打卡時刻 < OVERNIGHT_END（06:00）
          2. Day N-1 只有 1 筆打卡，且時刻 ≥ OVERNIGHT_START（16:00）
          → 將 Day N 最早的清晨打卡的 punch_date 改為 Day N-1。
          → 若 Day N 移走後已無任何打卡，則標記 Day N 為「已吸收」。

        注意：Day N 可能同時有跨夜下班卡（02:00）和當天正常上班卡（08:00），
        此時只移走 02:00，Day N 仍保留 08:00/17:00 當天的記錄。

        Returns:
            (修改後的 DataFrame, 被吸收日期的 set)
        """
        date_to_idx: dict = {}
        for idx, row in employee_df.iterrows():
            d = row['punch_date']
            date_to_idx.setdefault(d, []).append(idx)

        sorted_dates = sorted(date_to_idx.keys())
        absorbed: set = set()
        reassign: dict = {}  # row_idx → new punch_date

        for i, d in enumerate(sorted_dates):
            if i == 0:
                continue
            idxs = date_to_idx[d]
            # 找出當日最早一筆打卡
            earliest_idx = min(idxs, key=lambda ix: employee_df.at[ix, 'punch_time'])
            earliest_t = employee_df.at[earliest_idx, 'punch_time']
            if earliest_t >= self.OVERNIGHT_END:
                continue  # 最早打卡不是清晨，不考慮跨夜
            prev_d = sorted_dates[i - 1]
            prev_idxs = date_to_idx[prev_d]
            if len(prev_idxs) != 1:
                continue
            prev_t = employee_df.at[prev_idxs[0], 'punch_time']
            if prev_t < self.OVERNIGHT_START:
                continue  # 前一天不是傍晚/夜班開始
            # 縫合：只移走清晨那一筆
            reassign[earliest_idx] = prev_d
            # 若 Day N 只剩這一筆（移走後無剩餘），整日吸收
            if len(idxs) == 1:
                absorbed.add(d)

        if reassign:
            employee_df = employee_df.copy()
            for idx, new_date in reassign.items():
                employee_df.at[idx, 'punch_date'] = new_date

        return employee_df, absorbed

    def _analyze_employee_attendance(
        self,
        employee_name: str,
        employee_df: pd.DataFrame
    ) -> AttendanceResult:
        """
        分析單一員工的考勤記錄
        """
        # 取得該員工的班表設定
        schedule = self.employee_schedules.get(employee_name, {})
        work_start_str = schedule.get('work_start', self.DEFAULT_WORK_START)
        work_end_str = schedule.get('work_end', self.DEFAULT_WORK_END)

        work_start = datetime.strptime(work_start_str, "%H:%M").time()
        work_end = datetime.strptime(work_end_str, "%H:%M").time()

        # 加上寬限時間
        grace_time = (
            datetime.combine(datetime.today(), work_start) +
            timedelta(minutes=self.LATE_GRACE_MINUTES)
        ).time()

        # ── 跨夜班縫合 ──────────────────────────────────────────────────
        employee_df, absorbed_dates = self._stitch_overnight_punches(employee_df)

        # 按日期重新分組（縫合後 punch_date 可能改變）
        grouped = employee_df.groupby('punch_date')

        # 找出整份上傳檔案的最早與最晚日期
        _min_date = employee_df['punch_date'].min() if not employee_df.empty else datetime.today().date()
        _max_date = employee_df['punch_date'].max() if not employee_df.empty else datetime.today().date()

        # 建立這段期間所有的工作日，排除被吸收的日期（跨夜班次日不獨立計算）
        expected_dates = []
        current_date = _min_date
        while current_date <= _max_date:
            if current_date.weekday() < 5 and current_date not in absorbed_dates:
                expected_dates.append(current_date)
            current_date += timedelta(days=1)

        details = []
        late_count = 0
        early_leave_count = 0
        missing_punch_in_count = 0
        missing_punch_out_count = 0
        total_late_minutes = 0
        total_early_minutes = 0
        normal_days = 0

        for check_date in expected_dates:
            day_records = (
                grouped.get_group(check_date).sort_values('punch_datetime')
                if check_date in grouped.groups
                else pd.DataFrame()
            )

            # 用 datetime 排序配對（支援跨夜班 punch_out 在次日），再取首/末
            punch_datetimes = sorted(day_records['punch_datetime'].tolist()) if not day_records.empty else []
            punch_in_dt  = punch_datetimes[0]  if len(punch_datetimes) >= 1 else None
            punch_out_dt = punch_datetimes[-1] if len(punch_datetimes) >= 2 else None

            # 保留 time 供顯示用（punch_out 可能是次日 datetime，取 .time() 仍合理）
            punch_in  = punch_in_dt.time()  if punch_in_dt  else None
            punch_out = punch_out_dt.time() if punch_out_dt else None

            # 判斷是否跨夜（下班打卡在隔日）
            is_overnight = (
                punch_out_dt is not None and
                punch_out_dt.date() > check_date
            )

            day_detail = {
                'date': check_date,
                'punch_in': punch_in,
                'punch_out': punch_out,
                'punch_in_dt': punch_in_dt,
                'punch_out_dt': punch_out_dt,
                'is_late': False,
                'is_early_leave': False,
                'is_missing_punch_in': False,
                'is_missing_punch_out': False,
                'late_minutes': 0,
                'early_minutes': 0,
                'status': 'normal'
            }

            # ── 檢查上班打卡 ───────────────────────────────────────────
            if punch_in is None:
                day_detail['is_missing_punch_in'] = True
                day_detail['status'] = 'missing_punch_in'
                missing_punch_in_count += 1
            elif punch_in > grace_time:
                day_detail['is_late'] = True
                day_detail['status'] = 'late'
                late_count += 1
                work_start_dt = datetime.combine(check_date, work_start)
                late_minutes = int((punch_in_dt - work_start_dt).total_seconds() / 60)
                day_detail['late_minutes'] = max(0, late_minutes)
                total_late_minutes += day_detail['late_minutes']

            # ── 檢查下班打卡 ───────────────────────────────────────────
            if len(punch_datetimes) < 2:
                day_detail['is_missing_punch_out'] = True
                day_detail['status'] = (
                    'missing_punch_out' if day_detail['status'] == 'normal'
                    else day_detail['status'] + '+missing_punch_out'
                )
                missing_punch_out_count += 1
            else:
                # 跨夜班：work_end 比較基準日調整到隔日
                if is_overnight:
                    work_end_cmp = datetime.combine(check_date + timedelta(days=1), work_end)
                else:
                    work_end_cmp = datetime.combine(check_date, work_end)

                if punch_out_dt < work_end_cmp:
                    day_detail['is_early_leave'] = True
                    day_detail['status'] = (
                        'early_leave' if day_detail['status'] == 'normal'
                        else day_detail['status'] + '+early_leave'
                    )
                    early_leave_count += 1
                    early_minutes = int((work_end_cmp - punch_out_dt).total_seconds() / 60)
                    day_detail['early_minutes'] = max(0, early_minutes)
                    total_early_minutes += day_detail['early_minutes']

            if day_detail['status'] == 'normal':
                normal_days += 1

            details.append(day_detail)

        return AttendanceResult(
            employee_name=employee_name,
            total_days=len(details),
            normal_days=normal_days,
            late_count=late_count,
            early_leave_count=early_leave_count,
            missing_punch_in_count=missing_punch_in_count,
            missing_punch_out_count=missing_punch_out_count,
            total_late_minutes=total_late_minutes,
            total_early_minutes=total_early_minutes,
            details=details
        )
    
    def generate_anomaly_report(
        self, 
        results: Dict[str, AttendanceResult]
    ) -> pd.DataFrame:
        """
        產生異常清單報表
        
        Args:
            results: 考勤分析結果
        
        Returns:
            異常清單 DataFrame
        """
        anomaly_records = []
        
        for employee_name, result in results.items():
            for detail in result.details:
                if detail['status'] != 'normal':
                    anomaly_records.append({
                        '員工姓名': employee_name,
                        '日期': detail['date'],
                        '上班打卡': detail['punch_in'].strftime("%H:%M") if detail['punch_in'] else '未打卡',
                        '下班打卡': detail['punch_out'].strftime("%H:%M") if detail['punch_out'] else '未打卡',
                        '狀態': detail['status'],
                        '遲到分鐘': detail['late_minutes'],
                        '早退分鐘': detail['early_minutes']
                    })
        
        return pd.DataFrame(anomaly_records)
    
    def generate_summary_report(
        self, 
        results: Dict[str, AttendanceResult]
    ) -> pd.DataFrame:
        """
        產生考勤統計摘要
        """
        summary_records = []
        
        for employee_name, result in results.items():
            summary_records.append({
                '員工姓名': employee_name,
                '總出勤天數': result.total_days,
                '正常天數': result.normal_days,
                '遲到次數': result.late_count,
                '早退次數': result.early_leave_count,
                '未打卡(上班)': result.missing_punch_in_count,
                '未打卡(下班)': result.missing_punch_out_count,
                '遲到總分鐘': result.total_late_minutes,
                '早退總分鐘': result.total_early_minutes
            })
        
        return pd.DataFrame(summary_records)


def parse_attendance_file(
    file_path: str,
    employee_schedules: Dict[str, dict] = None,
    name_column: str = "姓名",
    datetime_column: str = "時間"
) -> Tuple[Dict[str, AttendanceResult], pd.DataFrame, pd.DataFrame]:
    """
    便捷函數：解析打卡記錄並回傳完整結果
    
    Args:
        file_path: Excel 檔案路徑
        employee_schedules: 員工班表設定
        name_column: 姓名欄位名稱
        datetime_column: 時間欄位名稱
    
    Returns:
        (分析結果, 異常清單, 統計摘要)
    """
    parser = AttendanceParser(employee_schedules)
    results = parser.parse_attendance_excel(
        file_path, 
        name_column=name_column,
        datetime_column=datetime_column
    )
    
    anomaly_report = parser.generate_anomaly_report(results)
    summary_report = parser.generate_summary_report(results)
    
    return results, anomaly_report, summary_report


# 測試用範例
if __name__ == "__main__":
    # 建立測試資料
    test_data = {
        '姓名': ['張小明', '張小明', '張小明', '張小明', '李小華', '李小華', '李小華', '李小華'],
        '時間': [
            '2026-02-02 08:05:00',  # 正常
            '2026-02-02 17:30:00',
            '2026-02-03 08:20:00',  # 遲到
            '2026-02-03 17:00:00',
            '2026-02-02 08:00:00',  # 正常
            '2026-02-02 16:30:00',  # 早退
            '2026-02-03 07:55:00',  # 正常
            '2026-02-03 17:05:00',
        ]
    }
    
    test_df = pd.DataFrame(test_data)
    test_file = '/tmp/test_attendance.xlsx'
    test_df.to_excel(test_file, index=False)
    
    # 測試解析
    results, anomaly, summary = parse_attendance_file(test_file)
    
    print("=== 考勤統計摘要 ===")
    print(summary)
    print("\n=== 異常清單 ===")
    print(anomaly)
