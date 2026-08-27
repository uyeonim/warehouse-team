import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

COSS_BASE_URL = "https://onem2m.iotcoss.ac.kr/Mobius"
AE_NAME = "ae_sjuiot"
CNT_NAME = "cnt_replace"
CNT_REPLACE_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"

ORIGIN = os.getenv("COSS_ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RI": "test_replace_001",
    "X-M2M-RVI": "3",
    "Content-Type": "application/vnd.onem2m-res+json;ty=4",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

# storage_id 0~3: rail 0~7 (글자 2개 x 색상 4개[r,y,b,g])
# storage_id 4(board): rail 0~3 (색상만)
replace_content = {"storage_id": 0, "rail": 5}  # 예시: storage0(EI)의 I-y

body = {"m2m:cin": {"con": json.dumps(replace_content)}}

res = requests.post(CNT_REPLACE_PATH, headers=headers, data=json.dumps(body))
print("Status Code:", res.status_code)
print("Response:", res.text)