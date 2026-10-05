import requests
import json

BENZINGA_API_KEY = "bz.ERYA3K4W3D4RKWDFSIQGE44YIHRJ6L5M"

params = {
    "token":         BENZINGA_API_KEY,
    "tickers":       "NVDA",
    "dateFrom":      "2026-07-17",
    "dateTo":        "2026-07-18",
    "displayOutput": "abstract",
    "pageSize":      50,
}

resp = requests.get(
    "https://api.benzinga.com/api/v2/news",
    params=params,
    headers={"Accept": "application/json"},
    timeout=10
)

print("Status code:", resp.status_code)
print("Response headers:", dict(resp.headers))
print("Response body:", json.dumps(resp.json(), indent=2)[:2000])