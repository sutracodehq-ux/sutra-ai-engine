import urllib.request
import json

MASTER_KEY = "sk_master_seLjIlhHzhO7KGBI-8erO7PGSVDgdHx4-Ht7KSPbgW8"
BASE_URL = "http://localhost:8090/v1"
HEADERS = {
    "Authorization": f"Bearer {MASTER_KEY}",
    "Content-Type": "application/json"
}

# 1. Get tenants
req = urllib.request.Request(f"{BASE_URL}/tenants", headers=HEADERS)
try:
    with urllib.request.urlopen(req) as response:
        tenants_data = json.loads(response.read().decode())
        tenant = next((t for t in tenants_data['tenants'] if t['slug'] == 'sutracode'), None)
except Exception as e:
    print(f"Error getting tenants: {e}")
    exit(1)

if not tenant:
    print("Tenant sutracode not found")
    exit(1)

# 2. Create new API key
data = json.dumps({
    "environment": "live",
    "tier": "standard",
    "label": "Frontend UI Key",
    "scopes": []
}).encode("utf-8")

req = urllib.request.Request(f"{BASE_URL}/tenants/{tenant['id']}/api-keys", data=data, headers=HEADERS, method="POST")
try:
    with urllib.request.urlopen(req) as response:
        key_data = json.loads(response.read().decode())
        new_key = key_data["api_key"]
        print(f"NEW_KEY={new_key}")
except Exception as e:
    print(f"Error creating key: {e}")
    exit(1)
