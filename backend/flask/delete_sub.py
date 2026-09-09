import os
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("COSS_BASE_URL").rstrip("/")
AE   = os.getenv("AE_NAME")

headers = {
    "X-API-KEY": os.getenv("COSS_API_KEY"),
    "X-AUTH-CUSTOM-LECTURE": os.getenv("COSS_LECTURE"),
    "X-AUTH-CUSTOM-CREATOR": os.getenv("COSS_CREATOR"),
    "X-M2M-Origin": os.getenv("ORIGIN"),
    "X-M2M-RI": f"del-{uuid.uuid4().hex}",
    "X-M2M-RVI": "3",
    "Accept": "application/json",
}

for cnt in ["cnt_order", "cnt_replace", "cnt_load"]:
    r = requests.delete(f"{BASE}/{AE}/{cnt}/flask_sub", headers=headers, timeout=15)
    print(f"{cnt}: {r.status_code}")   # 200 삭제됨, 404 원래 없었음