"""
考勤解析引擎 - 解析打卡記錄 Excel 並計算遲到/未打卡次數
"""

import pandas as pd
from datetime import datetime, time, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import os


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
        
        # 按日期分組
        grouped = employee_df.groupby('punch_date')
        
        details = []
        late_count = 0
        early_leave_count = 0
        missing_punch_in_count = 0
        missing_punch_out_count = 0
        total_late_minutes = 0
        total_early_minutes = 0
        normal_days = 0
        
        for date, day_records in grouped:
            day_records = day_records.sort_values('punch_datetime')
            punch_times = day_records['punch_time'].tolist()
            
            # 判斷上班打卡（第一筆）
            punch_in = punch_times[0] if len(punch_times) >= 1 else None
            # 判斷下班打卡（最後一筆，如果有多筆的話）
            punch_out = punch_times[-1] if len(punch_times) >= 2 else None
            
            day_detail = {
                'date': date,
                'punch_in': punch_in,
                'punch_out': punch_out,
                'is_late': False,
                'is_early_leave': False,
                'is_missing_punch_in': False,
                'is_missing_punch_out': False,
                'late_minutes': 0,
                'early_minutes': 0,
                'status': 'normal'
            }
            
            # 檢查上班打卡
            if punch_in is None:
                day_detail['is_missing_punch_in'] = True
                day_detail['status'] = 'missing_punch_in'
                missing_punch_in_count += 1
            elif punch_in > grace_time:
                # 遲到
                day_detail['is_late'] = True
                day_detail['status'] = 'late'
                late_count += 1
                
                # 計算遲到分鐘數
                punch_in_dt = datetime.combine(datetime.today(), punch_in)
                work_start_dt = datetime.combine(datetime.today(), work_start)
                late_minutes = int((punch_in_dt - work_start_dt).total_seconds() / 60)
                day_detail['late_minutes'] = max(0, late_minutes)
                total_late_minutes += day_detail['late_minutes']
            
            # 檢查下班打卡
            if len(punch_times) < 2:
                day_detail['is_missing_punch_out'] = True
                if day_detail['status'] == 'normal':
                    day_detail['status'] = 'missing_punch_out'
                else:
                    day_detail['status'] += '+missing_punch_out'
                missing_punch_out_count += 1
            elif punch_out < work_end:
                # 早退
                day_detail['is_early_leave'] = True
                if day_detail['status'] == 'normal':
                    day_detail['status'] = 'early_leave'
                else:
                    day_detail['status'] += '+early_leave'
                early_leave_count += 1
                
                # 計算早退分鐘數
                punch_out_dt = datetime.combine(datetime.today(), punch_out)
                work_end_dt = datetime.combine(datetime.today(), work_end)
                early_minutes = int((work_end_dt - punch_out_dt).total_seconds() / 60)
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
                        '上班打卡': str(detail['punch_in']) if detail['punch_in'] else '未打卡',
                        '下班打卡': str(detail['punch_out']) if detail['punch_out'] else '未打卡',
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
