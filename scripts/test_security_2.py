import requests
import json

BASE_URL = "http://localhost:8000/api"

try:
    r = requests.post(f"{BASE_URL}/auth/token", data={"username": "admin", "password": "password"}, timeout=5)
    token = r.json().get("access_token")
    headers = {"Authorization": f"Bearer {token}"}

    print("=== 1. Test Excel Export Sanitization ===")
    r = requests.get(f"{BASE_URL}/exports/employees", headers=headers, timeout=5)
    print(f"Export Employees status: {r.status_code}")

    print("\n=== 2. Test Negative Portal Hours ===")
    payload = {
        "overtime_date": "2026-03-01",
        "overtime_type": "weekday",
        "hours": -5.0,
        "reason": "Test"
    }
    r = requests.post(f"{BASE_URL}/portal/my-overtimes", json=payload, headers=headers, timeout=5)
    print(f"Portal Negative Overtime Creation status: {r.status_code}")
    if r.status_code == 422:
        print("SUCCESS: Rejected by Pydantic validation")
        print(r.json())
    else:
        print("FAILED: Did not get 422")
except Exception as e:
    print(f"Error: {e}")
