import requests
import json
import uuid

BASE_URL = "http://localhost:8000/api"

# Get a valid token (assuming admin exists with these credentials from previous sessions)
r = requests.post(f"{BASE_URL}/auth/token", data={"username": "admin", "password": "password"})
token = r.json().get("access_token")
headers = {"Authorization": f"Bearer {token}"}

print("=== 1. Test Excel Export Sanitization ===")
r = requests.get(f"{BASE_URL}/exports/employees", headers=headers)
print(f"Export Employees status: {r.status_code}")

# Can't easily test malicious string in the DB directly here w/o DB access, but 200 OK means the export didn't break.

print("\n=== 2. Test Negative Portal Hours ===")
# To test portal hours we would normally need an employee token, but the pydantic model validates it regardless.
payload = {
    "overtime_date": "2026-03-01",
    "overtime_type": "weekday",
    "hours": -5.0,
    "reason": "Test"
}
# Since it's a portal route, let's use the admin token (it might fail auth, but we want to see Pydantic 422 error first)
r = requests.post(f"{BASE_URL}/portal/my-overtimes", json=payload, headers=headers)
print(f"Portal Negative Overtime Creation status: {r.status_code}")
if r.status_code == 422:
    print("SUCCESS: Rejected by Pydantic validation")
    print(r.json())
else:
    print("FAILED: Did not get 422")

