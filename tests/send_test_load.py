import os
import requests
import json
import random
import string
import sys
from dotenv import load_dotenv

load_dotenv()

COSS_BASE_URL = "https://onem2m.iotcoss.ac.kr/Mobius"
AE_NAME = "ae_sjuiot"
CNT_NAME = "cnt_load"
CNT_LOAD_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"

ORIGIN = os.getenv("COSS_ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RI": "test_load_" + "".join(random.choices(string.digits, k=6)),
    "X-M2M-RVI": "3",
    "Content-Type": "application/vnd.onem2m-res+json;ty=4",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

# 사용법: python send_test_load.py order_test_1234
order_id = sys.argv[1] if len(sys.argv) > 1 else "order_test_0000"

load_content = {
    "order_id": order_id,
    "station_id": "2",
    "status": "loaded"
}

body = {
    "m2m:cin": {
        "con": json.dumps(load_content)
    }
}

print("cnt_load 전송(AGV 시뮬레이션):", json.dumps(load_content, ensure_ascii=False))

res = requests.post(CNT_LOAD_PATH, headers=headers, data=json.dumps(body))
print("Status Code:", res.status_code)
print("Response:", res.text)