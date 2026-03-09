
import holidays
found = False
try:
    tw = holidays.country_holidays('TW', years=2026, language='zh_TW')
    print("Trying language='zh_TW'")
    for date, name in tw.items():
        print(f"{date}: {name}")
        found = True
        break
except Exception as e:
    print(f"zh_TW failed: {e}")

if not found:
    try:
        tw = holidays.country_holidays('TW', years=2026, language='zh-TW')
        print("Trying language='zh-TW'")
        for date, name in tw.items():
            print(f"{date}: {name}")
            found = True
            break
    except Exception as e:
        print(f"zh-TW failed: {e}")
