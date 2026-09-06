import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

COSS_BASE_URL = os.getenv("COSS_BASE_URL", "https://onem2m.iotcoss.ac.kr/Mobius")
AE_NAME = os.getenv("AE_NAME")
CNT_NAME = "cnt_process"
CNT_PROCESS_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"
SUB_PATH = f"{CNT_PROCESS_PATH}/sub_process_notify"

ORIGIN = os.getenv("ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

# Cloud Run 배포 URL - .env의 FLASK_BASE_URL 기준으로 조합
# 주의: app.py 라우트가 /notify/<kind> 형태라 실제 경로는 /notify/process 임
#       (/notify/cnt_process 아님 - TOPICS 딕셔너리의 키가 "process"이기 때문)
FLASK_BASE_URL = os.getenv("FLASK_BASE_URL")
FLASK_PUBLIC_URL = f"{FLASK_BASE_URL}/notify/process"

common_headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RVI": "3",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

# 1) 기존 구독 삭제 (없으면 실패해도 무시)
del_headers = {**common_headers, "X-M2M-RI": "sub_process_del_001"}
del_res = requests.delete(SUB_PATH, headers=del_headers)
print("삭제 시도 결과:", del_res.status_code, del_res.text)

# 2) net:[3] = Create of Direct Child Resource 로 재등록
create_headers = {
    **common_headers,
    "X-M2M-RI": "sub_process_req_002",
    "Content-Type": "application/vnd.onem2m-res+json;ty=23"
}

body = {
    "m2m:sub": {
        "rn": "sub_process_notify",
        "nu": [FLASK_PUBLIC_URL],
        "nct": 1,
        "enc": {
            "net": [3]
        }
    }
}

res = requests.post(CNT_PROCESS_PATH, headers=create_headers, data=json.dumps(body))
print("Status Code:", res.status_code)
print("Response:", res.text)
print("등록된 nu 주소:", FLASK_PUBLIC_URL)