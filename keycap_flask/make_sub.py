import os
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

# [2026-11-01 로컬 전환] Cloud Run -> 창고 PC(192.168.0.12)의 키캡 Flask(5002)로 알림 받음
# 구독 이름은 클라우드 Mobius에서 쓰던 sub_warehouse_keycap_cartridge 그대로 (보드 구독 sub_warehouse_robotarm과 별개)
# 같은 이름이 있으면 지우고 다시 만듦 (이 이름만 지움 - 다른 팀 구독은 안 건드림)
BASE = os.getenv("COSS_BASE_URL", "http://192.168.0.100:7579/Mobius").rstrip("/")
AE   = os.getenv("AE_NAME")
NOTIFY_BASE_URL = os.getenv("NOTIFY_BASE_URL", "http://192.168.0.12:5002").rstrip("/")
SUB_RN = "sub_warehouse_keycap_cartridge"

# order/replace/load는 cnt_process 하나로 통합되면서 폐기됨.
SUBS = {
    "cnt_process": "process",
}

def headers(create=False):
    h = {
        "X-M2M-Origin": os.getenv("ORIGIN"),
        "X-M2M-RI": f"sub-{uuid.uuid4().hex}",
        "X-M2M-RVI": "3",
        "Accept": "application/json",
    }
    if create:
        h["Content-Type"] = "application/vnd.onem2m-res+json; ty=23"
    if os.getenv("COSS_API_KEY"):  # 로컬 Mobius는 인증 헤더 불필요 - 값이 있을 때만
        h["X-API-KEY"] = os.getenv("COSS_API_KEY")
        h["X-AUTH-CUSTOM-LECTURE"] = os.getenv("COSS_LECTURE")
        h["X-AUTH-CUSTOM-CREATOR"] = os.getenv("COSS_CREATOR")
    return h

for cnt, kind in SUBS.items():
    nu = f"{NOTIFY_BASE_URL}/notify/{kind}"
    d = requests.delete(f"{BASE}/{AE}/{cnt}/{SUB_RN}", headers=headers(), timeout=15)
    print(f"{cnt}/{SUB_RN} 삭제 시도: {d.status_code}" + (" (없음 - 정상)" if d.status_code == 404 else ""))
    body = {"m2m:sub": {
        "rn": SUB_RN,
        "nu": [nu],
        "enc": {"net": [3]},
        "nct": 1,
    }}
    r = requests.post(f"{BASE}/{AE}/{cnt}", headers=headers(create=True), json=body, timeout=15)
    print(f"{cnt}/{SUB_RN} 생성: {r.status_code} -> {nu}", "" if r.status_code == 201 else r.text)
