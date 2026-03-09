import pandas as pd
from datetime import date
from services.attendance_parser import AttendanceParser

# Create a fake upload dataframe that MISSES a middle weekday (e.g. 2026-03-03 Tue)
data = {
    'Department': ['Admin', 'Admin'],
    'Name': ['Test User', 'Test User'],
    'Date': ['2026/03/02', '2026/03/04'],
    'Time': ['08:00', '17:00'],
}
df = pd.DataFrame(data)

parser = AttendanceParser()
try:
    results = parser.parse(df)
    res = results['Test User']
    
    print(f"Total parsed days: {res.total_days}")
    missing = [d for d in res.details if d['status'] == 'missing_punch_in+missing_punch_out' or d['is_missing_punch_in']]
    print(f"Detected missing punches on dates:")
    for m in missing:
        print(f" - {m['date']}: {m['status']}")
        
    if len(missing) >= 1:
        print("SUCCESS: Ghost day successfully detected!")
    else:
        print("FAILED: Did not detect the ghost day on 03/03.")
except Exception as e:
    print("Error:", e)
