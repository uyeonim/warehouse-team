import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

COSS_BASE_URL = "https://onem2m.iotcoss.ac.kr/Mobius"
AE_NAME = "ae_sjuiot"
CNT_NAME = "cnt_order"
CNT_ORDER_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"
SUB_PATH = f"{CNT_ORDER_PATH}/sub_order_notify"

ORIGIN = os.getenv("COSS_ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

# Cloud Run 배포 URL - .env의 FLASK_BASE_URL 기준으로 조합
FLASK_BASE_URL = os.getenv("FLASK_BASE_URL")
FLASK_PUBLIC_URL = f"{FLASK_BASE_URL}/notify/cnt_order"

common_headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RVI": "3",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

# 1) 기존 구독 삭제 (없으면 실패해도 무시)
del_headers = {**common_headers, "X-M2M-RI": "sub_del_001"}
del_res = requests.delete(SUB_PATH, headers=del_headers)
print("삭제 시도 결과:", del_res.status_code, del_res.text)

# 2) net:[3] = Create of Direct Child Resource 로 재등록
create_headers = {
    **common_headers,
    "X-M2M-RI": "sub_req_002",
    "Content-Type": "application/vnd.onem2m-res+json;ty=23"
}

body = {
    "m2m:sub": {
        "rn": "sub_order_notify",
        "nu": [FLASK_PUBLIC_URL],
        "nct": 1,
        "enc": {
            "net": [3]
        }
    }
}

res = requests.post(CNT_ORDER_PATH, headers=create_headers, data=json.dumps(body))
print("Status Code:", res.status_code)
print("Response:", res.text)