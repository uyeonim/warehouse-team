"""
Mobius cnt_process 구독(sub_warehouse_robotarm) 재등록 - 한 번만 실행하는 설정용.
실행: python tools/sub_robotarm.py  (확인 질문에 y 입력해야 실제로 삭제/재생성)
import만 해서는 아무것도 안 함.
⚠️ main_board_robot_arm.py가 켜진 상태에서 실행할 것 - 등록할 때 Mobius가 알림 주소로
   확인 요청(vrq)을 보내는데, 받는 쪽이 없으면 등록이 실패할 수 있음.
"""
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

# 알림 받을 주소 = 로봇암 PC의 main 수신 서버. 예) http://192.168.0.50:5001
# (Mobius 서버에서 접근 가능한 IP여야 함 - 127.0.0.1 안 됨)
NOTIFY_BASE_URL = os.getenv("NOTIFY_BASE_URL")
NOTIFY_URL = f"{(NOTIFY_BASE_URL or '').rstrip('/')}/notify/process"

common_headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RVI": "3",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

def main():
    if not NOTIFY_BASE_URL:
        raise SystemExit(".env에 NOTIFY_BASE_URL이 비어있음 - 중단 (빈 주소로 구독 만들면 알림이 안 옴)")

    print(f"구독 rn  : {SUB_RN} (팀원 구독 sub_process_notify와 별개)")
    print(f"대상 cnt : {AE_NAME}/{CNT_NAME}")
    print(f"알림 주소: {NOTIFY_URL}")
    if input("이 구독을 삭제 후 재생성합니다. 계속할까요? (y/n): ").strip().lower() != "y":
        print("취소 - 아무것도 안 바꿈")
        return

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
            "nu": [NOTIFY_URL],
            "nct": 1,
            "enc": {
                "net": [3]
            }
        }
    }

    res = requests.post(CNT_PROCESS_PATH, headers=create_headers, data=json.dumps(body))
    print("Status Code:", res.status_code)
    print("Response:", res.text)
    print("등록된 nu 주소:", NOTIFY_URL)
    print("구독 rn:", SUB_RN, "(팀원 구독 sub_process_notify와 별개)")


if __name__ == "__main__":
    main()
