import os
import requests
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))  # 루트의 .env

COSS_BASE_URL = os.getenv("COSS_BASE_URL", "https://onem2m.iotcoss.ac.kr/Mobius")
AE_NAME = os.getenv("AE_NAME")
CNT_NAME = "cnt_process"
CNT_PROCESS_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"

# ⚠️ 핵심: 이 rn이 팀원 구독(sub_process_notify)과 겹치면 안 됨.
# 같은 이름으로 만들면 기존 구독을 삭제하고 덮어써서 키캡 팀 릴레이가 끊김.
SUB_RN = "sub_warehouse_robotarm"  # 팀원 구독과 겹치지 않게 유니크하게 지을 것
SUB_PATH = f"{CNT_PROCESS_PATH}/{SUB_RN}"

ORIGIN = os.getenv("ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

# 이 Flask(로봇암 릴레이) 자체의 Cloud Run URL - 배포 후 나온 값으로 .env에 채울 것
FLASK_BASE_URL = os.getenv("FLASK_BASE_URL")
FLASK_PUBLIC_URL = f"{FLASK_BASE_URL}/notify/process"

common_headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RVI": "3",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

# 1) 기존 구독 삭제 시도 (없으면 실패해도 무시) - SUB_RN이 유니크하므로
#    이 삭제는 로봇암 구독 자기 자신만 지움. 팀원 구독은 절대 안 건드림.
del_headers = {**common_headers, "X-M2M-RI": "sub_process_robotarm_del_001"}
del_res = requests.delete(SUB_PATH, headers=del_headers)
print("삭제 시도 결과:", del_res.status_code, del_res.text)

# 2) net:[3] = Create of Direct Child Resource 로 재등록
create_headers = {
    **common_headers,
    "X-M2M-RI": "sub_process_robotarm_req_002",
    "Content-Type": "application/vnd.onem2m-res+json;ty=23"
}

body = {
    "m2m:sub": {
        "rn": SUB_RN,
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
print("구독 rn:", SUB_RN, "(팀원 구독 sub_process_notify와 별개)")