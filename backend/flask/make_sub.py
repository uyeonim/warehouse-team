import os
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("COSS_BASE_URL").rstrip("/")
AE   = os.getenv("AE_NAME")
CLOUD_RUN = "https://warehouse-flask-53685477799.asia-northeast3.run.app"

# order/replace/load는 cnt_process 하나로 통합되면서 폐기됨.
# 기존 구독은 delete_sub.py로 지우는 걸 권장 (안 지워도 동작엔 지장 없지만
# Mobius가 계속 /notify/order 등으로 웹훅을 쏘다가 404만 받게 됨).
SUBS = {
    "cnt_process": "process",
}

def headers():
    return {
        "X-API-KEY": os.getenv("COSS_API_KEY"),
        "X-AUTH-CUSTOM-LECTURE": os.getenv("COSS_LECTURE"),
        "X-AUTH-CUSTOM-CREATOR": os.getenv("COSS_CREATOR"),
        "X-M2M-Origin": os.getenv("ORIGIN"),
        "X-M2M-RI": f"sub-{uuid.uuid4().hex}",
        "X-M2M-RVI": "3",
        "Content-Type": "application/vnd.onem2m-res+json; ty=23",
        "Accept": "application/json",
    }

for cnt, kind in SUBS.items():
    body = {"m2m:sub": {
        "rn": "flask_sub",
        "nu": [f"{CLOUD_RUN}/notify/{kind}"],
        "enc": {"net": [3]},
        "nct": 1,
    }}
    r = requests.post(f"{BASE}/{AE}/{cnt}", headers=headers(), json=body, timeout=15)
    print(f"{cnt}: {r.status_code}", "" if r.status_code == 201 else r.text)
