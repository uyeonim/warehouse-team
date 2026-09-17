# -*- coding: utf-8 -*-
"""
=====================================================================
 보드 로봇암 + 트레이 컨베이어 - DobotRPC 단일 + pydobot 스타일 이동
=====================================================================
⚠️ pydobot(시리얼 직결)과 DobotRPC(DobotLink 경유)는 같은 COM 포트를
   동시에 못 염(포트는 한 프로그램만 독점). 팔/컨베이어가 실제로 같은
   포트라서, 라이브러리를 진짜로 섞어 쓸 수는 없음 - 전부 DobotRPC로
   통일된 상태 유지.

   대신 "이동 방식"만 pydobot 버전(검증됨, 실물로 잘 됐던 방식)의
   전략을 그대로 가져옴:
     - GetPose로 엄격하게 "도착했는지" 확인하고 재시도하는 방식 (X)
       -> DobotRPC의 GetPose도 연속 호출 시 불안정한 게 실물로 확인됨
          ("초기위치로 복귀"가 매번 반복적으로 실패)
     - 이동 명령 보내고 고정 시간만큼 대기, 그 다음 위치는 "육안 확인용"
       으로만 한 번 조회 (O) - pydobot 버전이 실제로 안정적으로 동작했던 방식

   즉 흡착/컨베이어는 DobotRPC 그대로, "도착 확인 로직"만 pydobot 철학으로 교체.
   이동 자체(SetPTPCmd)는 여전히 DobotRPC를 씀 - pydobot 라이브러리 자체를
   불러오지는 않음(포트 충돌 때문에 불가능).

⚠️ 이 결과로 "실패 감지"가 약해짐: 이동이 실제로 안 됐어도 예외만 없으면
   성공으로 보고함(pydobot 버전과 동일한 트레이드오프). 필요해지면 센서
   기반 확인을 나중에 추가.

흡착 파라미터는 가이드북 API 레퍼런스로 확정된 대로 "on" 사용
(m_lite.set_endeffector_suctioncup(enable, on)).

MQTT 수신 / Flask 보고 로직은 이전과 동일 (app.py 수정 불필요).

주문 로직 - 키캡 디스펜서 아두이노(alreadyProcessed / jobQueue / 서버큐 /
pickup 게이트) 패턴과 동일하게 맞춤:
  - 중복방지: jobKey(order_id+color+status+retry) 최근 8개를 링버퍼로 기억,
    같은 스냅샷 재전송이나 assigned/tray_reached가 여러 스냅샷에 걸쳐
    남아있어도 큐에 두 번 안 넣음.
    ⚠️ jobKey에 status/retry를 포함시킨 이유: tray_reached(최초 픽업)와
    board_mismatched(재출고)가 같은 order_id+color를 쓰기 때문에, status를
    안 넣으면 재출고 요청이 "이미 처리한 job"으로 오인되어 무시당함.
    retry까지 넣은 이유는 재출고가 최대 2회 반복될 수 있는데(명세 8-2),
    두 번째 재출고도 status는 똑같이 board_mismatched라서 status만으로는
    구분이 안 됨 - 팀원 warehouse_server.py가 order.get("retry")로 이 문제를
    풀었던 것과 동일한 방식. retry 필드가 실제로 오는지는 미확인이라, 없으면
    None으로 처리되어 최초 1회 재출고까지는 정상 동작하고 그 이상 반복되는
    재출고 구분만 안 될 수 있음(플랫폼팀 확인 필요).
  - 로컬 큐 + 서버 큐(Redis, /queue/board/*) 동기화: tray_reached/
    board_mismatched 감지 시 즉시 처리하지 않고 큐에 적재 -> 재부팅해도
    GET /queue/board/next로 미완료 주문 복원 가능
  - 유령주문 정리: 큐에 남은 job의 order_id가 스냅샷 orders에서 사라지면
    로컬+서버 큐 양쪽에서 제거 (cnt_process 스펙상 진행 중 주문은 매
    스냅샷마다 전부 실리므로, 한 번 빠지면 종료된 것으로 간주 가능)
  - 픽업 게이트(안전망): 명세상 벨트가 주문 1건만 태우므로 tray_reached
    시점엔 이전 주문 흔적이 있을 수 없어 정상 흐름에선 게이트가 항상
    열려있는 상태로 즉시 통과함. 그래도 수동 테스트 스냅샷처럼 명세를
    벗어난 입력 대비 안전장치로 유지. 픽업+컨베이어 직후(성공/실패 무관)
    잠그고, 그 주문이 pickup_reached 이후 상태가 되거나 목록에서
    사라지면 열림 (/gate/board/*)
    ⚠️ GATE_OPEN_STATUSES는 키캡 코드 기준으로 옮긴 것 - 플랫폼 상태표와
    실제 철자/대소문자 일치 여부 확인 필요. unclaimed(노쇼) 포함 여부도
    노쇼 시 실제로 픽업 지점이 비워지는지에 달려있어 재검토 필요.

⚠️ 미확인 설계 질문 (플랫폼팀/이사님 확인 필요, 코드는 일단 "비어있다"고 가정):
  board_mismatched(재출고) 시점에 트레이 위에 이미 잘못된 보드가 놓여있는
  상태인지, 아니면 플랫폼/다른 파트가 이미 치운 상태인지 확인 안 됨.
  지금 pick_and_place_board_async는 트레이 자리가 비어있다는 전제로 그냥
  다시 놓기만 함 - 이미 뭔가 놓여있으면 충돌·겹침 가능성 있음.

전체 흐름
  [보드 픽업 - tray_reached / board_mismatched(재출고)]
    0. jobKey 중복체크 -> 로컬 큐 + 서버 큐 적재 (게이트 열렸을 때만 실제 처리)
    1. cnt_board_status/cnt_{색} = busy
    2. 배출 (A안 높이계산)
    3. cnt_robot_arm/cnt_board_robot_arm = done/failed
    4. 성공 시 cnt_warehouse/cnt_board_dispense
    5. cnt_board_status/cnt_{색} = idle + count-1  -> 플랫폼이 board_packed로 전이
    6. 게이트 잠금 + 서버 큐에서 제거
  [트레이 컨베이어 - keycap_packed(section1) / board_packed(section2)]
    7. 컨베이어(Magic Box) 해당 section 구간 구동 (게이트/큐 없음 - 직접 반응이라 불필요)
    8. cnt_conveyor/cnt_tray_conveyor = {"order_id":..., "sectionN": "done"|"failed"}

같은 (order_id, status) 조합은 한 번만 처리 (MQTT 재전송 중복 실행 방지) -
이건 컨베이어 트리거(keycap_packed/board_packed)에만 남겨둠. 픽업 트리거
(tray_reached/board_mismatched)는 위 jobKey 링버퍼 방식으로 대체.

실행 전 확인
  - DobotLab 연결 끊어둘 것 (COM 포트 충돌 방지)
  - DobotLink.exe 실행 중이어야 함
  - .env에 MQTT_HOST/PORT/USER/PASS, MQTT_TOPIC, FLASK_BASE_URL
  - pip install DobotRPC (Python 3.9 환경 - venv-dobot)
=====================================================================
"""

import asyncio
import json
import math
import os
import queue
import time

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv
from DobotRPC import RPCClient, DobotlinkAdapter

load_dotenv()

# ---------------------------------------------------------------
# MQTT 설정
# ---------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sjuiot/warehouse/robotarm")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "pc_robot_arm_board")

if not all([MQTT_HOST, MQTT_USER, MQTT_PASS]):
    raise RuntimeError(".env에 MQTT_HOST / MQTT_USER / MQTT_PASS가 비어있음")

FLASK_BASE_URL = os.getenv("FLASK_BASE_URL")
if not FLASK_BASE_URL:
    raise RuntimeError(".env에 FLASK_BASE_URL이 비어있음")
FLASK_BASE_URL = FLASK_BASE_URL.rstrip("/")
REPORT_TIMEOUT = 5

STATUS_PICK = "tray_reached"           # 보드 픽업 트리거
STATUS_MISMATCH = "board_mismatched"   # 재출고 트리거 - tray_reached와 같은 핸들러로 처리
STATUS_CONVEYOR_1 = "keycap_packed"    # 컨베이어 section1 - 보드 로봇암 앞까지 이동
STATUS_CONVEYOR_2 = "board_packed"     # 컨베이어 section2 - 픽업대까지 이동

DOBOT_PORT = "COM17"   # 팔 + 컨베이어(Magic Box) 공유 포트
NAMESPACE_ARM = "MagicianLite"
NAMESPACE_BOX = "MagicBox"

# =================================================================
# CONFIG - 로봇암 좌표 (실측 완료)
# =================================================================
PICK_POSE_BASE = {
    "red":    (248.1, 147.3137, -38.1, 0.0),
    "green":  (248.1,  41.7095, -38.1, 0.0),
    "blue":   (248.1, -51.7989, -38.1, 0.0),
    "yellow": (248.1, -137.9115, -38.1, 0.0),
}
BOARD_THICKNESS_MM = 20.0

PLACE_POSE = (189.7914, -280.7065, 21.5396, -82.0)
HOME_POSE = (200.0, 0.0, 40.0, 0.0)

# =================================================================
# CONFIG - 트레이 컨베이어 (⚠️ 캘리브레이션 스크립트로 확정 전 - 전부 placeholder)
# 물리적으로 벨트 2개(STP1/STP2)를 이어붙여 "하나의 긴 벨트"로 씀 - 이 두 모터는
# section1이든 section2든 항상 같이 구동/정지함 (아래 STP_1/STP_2가 그 둘).
#
# 그 "하나의 긴 벨트"가 두 구간을 담당함 (이건 모터 개수랑 무관한 별개의 구분):
#   section1: keycap_packed 감지 시 구동 - 보드 로봇암 앞까지 이동
#   section2: board_packed  감지 시 구동 - 픽업대까지 이동
# 두 구간 이동 거리가 다를 수 있어 구동 시간을 따로 둠 (속도는 공통값으로 시작)
# =================================================================
TRAY_CONVEYOR_STP_1 = 0   # Magic Box STP1 포트
TRAY_CONVEYOR_STP_2 = 0   # Magic Box STP2 포트 (벨트 1개만 연결된 동안은 STP1과 동일값)
TRAY_CONVEYOR_SPEED_MM_S = 30

TRAY_CONVEYOR_RUN_TIME_SECTION1 = 6.0   # keycap_packed -> 보드 로봇암 앞까지
TRAY_CONVEYOR_RUN_TIME_SECTION2 = 5.0   # board_packed -> 픽업대까지

# =================================================================
# CONFIG - 이동 타이밍 (pydobot 버전 검증값 참고)
# =================================================================
MOVE_WAIT_SEC = 3.0          # 이동 명령 후 고정 대기 시간 - 거리 크면 늘릴 것
SUCTION_SETTLE_DELAY = 1.0   # 흡착력 형성 대기
RELEASE_SETTLE_DELAY = 0.5
SAFE_LIFT_MM = 40.0

BOARD_COLORS = ["red", "yellow", "blue", "green"]
CALL_TIMEOUT = 5

_MM_TO_PULSE = (360.0 / 1.8 * 5.0 * 16.0) / (3.1415926535898 * 32)


def mm_per_sec_to_pulses(speed_mm_s):
    return int(math.floor(speed_mm_s * _MM_TO_PULSE))


snapshot_queue: "queue.Queue" = queue.Queue()
last_seq = -1
local_stock = {}
handled_status = {}   # (order_id -> status) - 컨베이어(keycap_packed/board_packed) 트리거 전용 dedup

# ---- 픽업 job 중복방지 (키캡 alreadyProcessed 패턴) ----
JOB_HISTORY_SIZE = 8
processed_job_keys = []  # 최근 처리(큐 적재)한 jobKey 링버퍼


def already_processed(job_key):
    return job_key in processed_job_keys


def mark_processed(job_key):
    processed_job_keys.append(job_key)
    if len(processed_job_keys) > JOB_HISTORY_SIZE:
        processed_job_keys.pop(0)


def build_job_key(order_id, color, status, retry):
    """
    tray_reached(최초)와 board_mismatched(재출고)가 같은 order_id+color를
    쓰므로 status를 반드시 포함해야 재출고가 "이미 처리한 job"으로 오인되어
    무시되는 걸 막을 수 있음. retry까지 포함하는 이유는 재출고가 최대 2회
    반복될 수 있는데(명세 8-2) 두 번째 재출고도 status는 똑같이
    board_mismatched라서 status만으로는 구분이 안 되기 때문 - retry 필드가
    없으면(None) 두 번째 재출고부터는 dedup에 걸려 무시될 수 있음, 플랫폼이
    실제로 retry를 내려주는지 확인 필요.
    """
    return f"{order_id}_{color}_{status}_{retry}"


# ---- 픽업 로컬 큐 (서버 큐와 동기화) ----
job_queue = []  # [{"order_id":..., "color":...}, ...] FIFO - pop(0)으로 소비

# ---- 픽업 게이트 (안전망) ----
gate_open = True
gate_order_id = ""

# pickup_reached 및 그 이후 전 단계 - 이 목록에 들면 게이트 오픈.
# ⚠️ 플랫폼 상태표 기준 철자 재확인 필요, unclaimed 포함 여부도 재검토 필요.
GATE_OPEN_STATUSES = (
    "pickup_reached", "loaded", "arrived", "unclaimed", "verified", "received", "completed"
)


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag:<8}] {msg}")


# ============================== MQTT 콜백 ==============================
def on_connect(client, userdata, flags, rc):
    log("MQTT", f"연결 결과 rc={rc}")
    if rc == 0:
        client.subscribe(MQTT_TOPIC, qos=1)
        log("MQTT", f"구독 완료: {MQTT_TOPIC}")
    else:
        log("MQTT", f"연결 실패 rc={rc}")


def on_disconnect(client, userdata, rc):
    log("MQTT", f"연결 끊김 rc={rc} - 자동 재연결 대기 중")


def on_message(client, userdata, msg):
    global last_seq
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError as e:
        log("MQTT", f"JSON 파싱 실패: {e}")
        return
    seq = data.get("seq", -1)
    if seq == last_seq:
        return
    last_seq = seq
    orders = data.get("orders", {})
    log("MQTT", f"새 스냅샷 수신 seq={seq}, 진행 중 주문={list(orders.keys())}")
    snapshot_queue.put(data)


def build_mqtt_client():
    client = mqtt.Client(client_id=MQTT_CLIENT_ID, transport="tcp")
    client.tls_set()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


# ============================== Flask 보고 헬퍼 ==============================
def fetch_initial_stock():
    try:
        resp = requests.get(f"{FLASK_BASE_URL}/stock/board", timeout=REPORT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        log("STOCK", f"초기 재고 로드: {data}")
        return {c: int(data.get(c, 0)) for c in BOARD_COLORS}
    except Exception as e:
        log("STOCK", f"초기 재고 로드 실패: {e} - 전부 0으로 시작")
        return {c: 0 for c in BOARD_COLORS}


def report_board_status(order_id, color, status, count=None):
    body = {"order_id": order_id, "board": color, "status": status}
    if count is not None:
        body["count"] = count
    try:
        resp = requests.post(f"{FLASK_BASE_URL}/report/board-status", json=body, timeout=REPORT_TIMEOUT)
        ok = 200 <= resp.status_code < 300
        log("REPORT", f"board-status {'성공' if ok else '실패'} ({resp.status_code}): {body}")
        return ok
    except Exception as e:
        log("REPORT", f"board-status 요청 실패: {e}")
        return False


def report_board_robot_arm(order_id, board_result):
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/report/board-robot-arm",
            json={"order_id": order_id, "board": board_result},
            timeout=REPORT_TIMEOUT,
        )
        ok = 200 <= resp.status_code < 300
        log("REPORT", f"board-robot-arm {'성공' if ok else '실패'} ({resp.status_code}): {board_result}")
        return ok
    except Exception as e:
        log("REPORT", f"board-robot-arm 요청 실패: {e}")
        return False


def report_board_dispense(order_id, color):
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/report/board-dispense",
            json={"order_id": order_id, "board": color},
            timeout=REPORT_TIMEOUT,
        )
        ok = 200 <= resp.status_code < 300
        log("REPORT", f"board-dispense {'성공' if ok else '실패'} ({resp.status_code}): {color}")
        return ok
    except Exception as e:
        log("REPORT", f"board-dispense 요청 실패: {e}")
        return False


def report_conveyor_tray(order_id, section, status):
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/report/conveyor/tray",
            json={"order_id": order_id, "section": section, "status": status},
            timeout=REPORT_TIMEOUT,
        )
        ok = 200 <= resp.status_code < 300
        log("REPORT", f"conveyor/tray(section{section}) {'성공' if ok else '실패'} ({resp.status_code}): {status}")
        return ok
    except Exception as e:
        log("REPORT", f"conveyor/tray 요청 실패: {e}")
        return False


# ---- 픽업 큐 서버 동기화 (키캡의 enqueueToServer/completeOnServer 대응) ----
def fetch_queue_from_server():
    try:
        resp = requests.get(f"{FLASK_BASE_URL}/queue/board/next", timeout=REPORT_TIMEOUT)
        resp.raise_for_status()
        items = resp.json()
        log("QUEUE", f"서버 큐 복원: {items}")
        return items
    except Exception as e:
        log("QUEUE", f"서버 큐 조회 실패: {e} - 빈 큐로 시작")
        return []


def enqueue_to_server(order_id, color):
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/queue/board/enqueue",
            json={"order_id": order_id, "color": color},
            timeout=REPORT_TIMEOUT,
        )
        return 200 <= resp.status_code < 300
    except Exception as e:
        log("QUEUE", f"서버 큐 적재 실패: {e}")
        return False


def complete_on_server(order_id, color):
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/queue/board/complete",
            json={"order_id": order_id, "color": color},
            timeout=REPORT_TIMEOUT,
        )
        return 200 <= resp.status_code < 300
    except Exception as e:
        log("QUEUE", f"서버 큐 제거 실패: {e}")
        return False


# ---- 픽업 게이트 서버 동기화 (키캡의 restoreGateFromServer 등 대응) ----
def fetch_gate_from_server():
    try:
        resp = requests.get(f"{FLASK_BASE_URL}/gate/board", timeout=REPORT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        log("GATE", f"서버 게이트 상태 복원: {data}")
        return data
    except Exception as e:
        log("GATE", f"서버 게이트 조회 실패: {e} - 기본값(열림)으로 시작")
        return {"locked": False}


def lock_gate_on_server(order_id):
    try:
        requests.post(f"{FLASK_BASE_URL}/gate/board", json={"order_id": order_id}, timeout=REPORT_TIMEOUT)
    except Exception as e:
        log("GATE", f"서버 게이트 잠금 실패: {e}")


def clear_gate_on_server():
    try:
        requests.post(f"{FLASK_BASE_URL}/gate/board/clear", timeout=REPORT_TIMEOUT)
    except Exception as e:
        log("GATE", f"서버 게이트 해제 실패: {e}")


# ============================== DobotRPC 공용 헬퍼 ==============================
async def safe_call(coro, timeout_sec=CALL_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def is_ok(res):
    return not (isinstance(res, str) and (res.startswith("TIMEOUT") or res.startswith("ERROR")))


# ============================== 이동 - pydobot 스타일 (고정 대기, 엄격 확인 없음) ==============================
async def move_and_wait(arm_api, pose, label=""):
    """
    SetPTPCmd 보내고 MOVE_WAIT_SEC초 고정 대기 (pydobot 버전 검증된 전략).
    ⚠️ GetPose로 "진짜 도착했는지" 재시도하며 확인하지 않음 - DobotRPC의
    GetPose도 연속 호출 시 불안정한 게 실물로 확인됨(재시도해도 똑같이 실패
    하는 패턴 반복). 위치는 로그용으로 한 번만 조회 (육안 확인용, 판정에는 안 씀).
    """
    x, y, z, r = pose
    moved = await safe_call(arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0,
                                               x=x, y=y, z=z, r=r, isQueued=False))
    await asyncio.sleep(MOVE_WAIT_SEC)

    actual = await safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False))
    log("ARM", f"{label} - 명령:{moved}, 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}), 실제위치:{actual}")
    return is_ok(moved)


async def suck(arm_api, on):
    """
    가이드북 API 레퍼런스로 파라미터명 확정: m_lite.set_endeffector_suctioncup(enable, on)
    """
    res = await safe_call(arm_api.SetEndEffectorSuctionCup(
        portName=DOBOT_PORT, enable=True, on=on, isQueued=False
    ))
    log("ARM", f"흡착 {'ON' if on else 'OFF'} 결과: {res}")
    return res


# ============================== 픽업 로직 ==============================
def compute_pick_pose(color, count):
    x, y, base_z, r = PICK_POSE_BASE[color]
    pick_z = base_z + (count - 1) * BOARD_THICKNESS_MM
    return (x, y, pick_z, r)


async def pick_and_place_board_async(arm_api, color, count):
    """
    픽업 -> 안전 높이로 들어올림 -> 안전 높이 유지한 채 이동 -> 트레이로 하강 -> 놓기
    -> 다시 안전 높이로 들어올림 -> 초기위치 복귀.
    ⚠️ pydobot 버전과 동일한 트레이드오프: 실제 도착을 엄격히 검증하지 않으므로,
    예외만 없으면 항상 성공으로 처리함. 로그의 "실제위치"로 육안 확인할 것.
    """
    pick_x, pick_y, pick_z, pick_r = compute_pick_pose(color, count)
    place_x, place_y, place_z, place_r = PLACE_POSE
    travel_z = max(pick_z, place_z) + SAFE_LIFT_MM
    log("ARM", f"[{color}] 픽업 좌표: ({pick_x:.1f},{pick_y:.1f},{pick_z:.1f}) / 이동 높이: {travel_z:.1f}")

    try:
        await move_and_wait(arm_api, (pick_x, pick_y, pick_z, pick_r), label="픽업 위치로 이동")

        grabbed = await suck(arm_api, True)
        await asyncio.sleep(SUCTION_SETTLE_DELAY)

        await move_and_wait(arm_api, (pick_x, pick_y, travel_z, pick_r), label="안전 높이로 들어올리기")
        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r), label="안전 높이 유지한 채 트레이 위로 이동")
        await move_and_wait(arm_api, (place_x, place_y, place_z, place_r), label="트레이 위치로 하강")

        released = await suck(arm_api, False)
        await asyncio.sleep(RELEASE_SETTLE_DELAY)

        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r), label="다시 안전 높이로 들어올리기")

        # ⚠️ 실험: "R -82 → 0 회전"과 "큰 X/Y 이동"을 한 번에 같이 시키면
        # 매번 조용히 무시되는 패턴이 실물로 반복 확인됨(목표=실제 완벽 일치했던
        # 이전 5개 이동과 달리, 회전+큰 이동이 겹치는 이 동작만 매번 실패).
        # 회전과 이동을 분리해서 순서대로 보내는 것으로 우회 시도.
        home_x, home_y, home_z, home_r = HOME_POSE
        await move_and_wait(arm_api, (place_x, place_y, travel_z, home_r), label="안전 높이에서 방향만 되돌리기")
        await move_and_wait(arm_api, HOME_POSE, label="초기위치로 복귀")

        log("ARM", "결과: 성공 (도착 좌표는 위 로그로 육안 확인할 것 - 자동 검증 없음)")
        return True

    except Exception as e:
        log("ARM", f"예외 발생 - 실패 처리: {e}")
        try:
            await suck(arm_api, False)  # 흡착 든 채로 죽는 것 방지
        except Exception:
            pass
        return False


# ============================== 컨베이어 로직 ==============================
async def _set_conveyor(box_api, index, enable, speed_mm_s):
    speed_pulses = mm_per_sec_to_pulses(speed_mm_s) if enable else 0
    return await safe_call(box_api.SetEMotor(
        portName=DOBOT_PORT, index=index, enable=enable, speed=speed_pulses, isQueued=False
    ))


async def run_tray_conveyor_async(box_api, run_time, section_label=""):
    """
    이어붙인 벨트 2개(STP1/STP2)를 거의 동시에 구동/정지, run_time초만큼.
    gather로 두 SetEMotor 호출을 동시에 쏴서 타이밍 어긋남을 최소화함 -
    순서대로 하나씩 보내면 두 번째 벨트가 몇백ms 늦게 시작/정지해서
    이음매에서 트레이가 걸리거나 밀릴 수 있음.
    """
    try:
        log("CONV", f"컨베이어 구동 시작{section_label} (STP1+STP2 동시, "
                     f"speed={TRAY_CONVEYOR_SPEED_MM_S}mm/s, {run_time}초)")
        start_res_1, start_res_2 = await asyncio.gather(
            _set_conveyor(box_api, TRAY_CONVEYOR_STP_1, True, TRAY_CONVEYOR_SPEED_MM_S),
            _set_conveyor(box_api, TRAY_CONVEYOR_STP_2, True, TRAY_CONVEYOR_SPEED_MM_S),
        )
        log("CONV", f"시작 명령 결과: STP1={start_res_1}, STP2={start_res_2}")

        await asyncio.sleep(run_time)

        stop_res_1, stop_res_2 = await asyncio.gather(
            _set_conveyor(box_api, TRAY_CONVEYOR_STP_1, False, 0),
            _set_conveyor(box_api, TRAY_CONVEYOR_STP_2, False, 0),
        )
        log("CONV", f"정지 명령 결과: STP1={stop_res_1}, STP2={stop_res_2}")

        return all(is_ok(r) for r in (start_res_1, start_res_2, stop_res_1, stop_res_2))
    except Exception as e:
        log("CONV", f"예외 발생: {e}")
        # 하나라도 켜진 채로 남는 것 방지 - 둘 다 강제 정지 시도
        try:
            await asyncio.gather(
                _set_conveyor(box_api, TRAY_CONVEYOR_STP_1, False, 0),
                _set_conveyor(box_api, TRAY_CONVEYOR_STP_2, False, 0),
            )
        except Exception:
            pass
        return False


# ============================== 주문 처리 (동기 래퍼) ==============================
def process_board_job(loop, arm_api, box_api, order_id, color):
    """
    큐에서 꺼낸 job 1건 처리 - 픽업 실행 후, 성공/실패 무관하게 게이트 잠금 + 서버 큐 제거.
    """
    global gate_open, gate_order_id

    if color not in BOARD_COLORS or color not in PICK_POSE_BASE:
        log("ARM", f"알 수 없는 색상 '{color}' - 중단")
        complete_on_server(order_id, color)
        return

    count_before = local_stock.get(color, 0)
    if count_before <= 0:
        log("ARM", f"[{color}] 재고 0 - 픽업 불가")
        report_board_status(order_id, color, "empty", count=0)
        report_board_robot_arm(order_id, "failed")
    else:
        report_board_status(order_id, color, "busy", count=count_before)
        success = loop.run_until_complete(pick_and_place_board_async(arm_api, color, count_before))
        report_board_robot_arm(order_id, "done" if success else "failed")

        if not success:
            report_board_status(order_id, color, "idle", count=count_before)
        else:
            report_board_dispense(order_id, color)
            count_after = count_before - 1
            local_stock[color] = count_after
            report_board_status(order_id, color, "idle", count=count_after)
            log("STOCK", f"[{color}] 재고 {count_before} -> {count_after}")

    # 성공/실패/재고없음 관계없이 - 이 주문의 픽업 시도는 끝났으므로 게이트 잠금 + 큐 정리
    gate_open = False
    gate_order_id = order_id
    lock_gate_on_server(order_id)
    log("GATE", f"잠금: {order_id} (pickup_reached 이후 대기)")
    complete_on_server(order_id, color)


def handle_tray_conveyor(loop, box_api, order_id, section):
    """section: "1" (keycap_packed -> 보드 로봇암 앞) 또는 "2" (board_packed -> 픽업대)."""
    run_time = TRAY_CONVEYOR_RUN_TIME_SECTION1 if section == "1" else TRAY_CONVEYOR_RUN_TIME_SECTION2
    log("CONV", f"{order_id}: 컨베이어 section{section} 구동 시작")
    success = loop.run_until_complete(run_tray_conveyor_async(box_api, run_time, f" (section{section})"))
    report_conveyor_tray(order_id, section, "done" if success else "failed")


# ---- 게이트: 기다리던 주문이 pickup_reached(이후) 됐는지 확인, 맞으면 게이트 열기 ----
def check_gate_against_orders(orders):
    global gate_open, gate_order_id

    if gate_open:
        return
    if not gate_order_id:
        gate_open = True
        return

    if gate_order_id not in orders:
        log("GATE", f"{gate_order_id} 목록에서 사라짐 -> 게이트 열림")
        gate_open = True
        gate_order_id = ""
        clear_gate_on_server()
        return

    status = orders[gate_order_id].get("status")
    if status in GATE_OPEN_STATUSES:
        log("GATE", f"{gate_order_id} status={status} -> 게이트 열림")
        gate_open = True
        gate_order_id = ""
        clear_gate_on_server()


# ---- 큐에 남은 job 중 orders에서 사라진 것(유령주문) 정리 ----
def prune_dead_jobs_from_queue(orders):
    global job_queue
    alive, removed = [], 0
    for job in job_queue:
        if job["order_id"] in orders:
            alive.append(job)
        else:
            complete_on_server(job["order_id"], job["color"])
            removed += 1
    job_queue = alive
    if removed:
        log("QUEUE", f"orders에서 사라진 job {removed}건 제거함")


def handle_snapshot(loop, arm_api, box_api, data):
    orders = data.get("orders", {})

    # 1. 게이트 재평가
    check_gate_against_orders(orders)

    # 2. 유령주문 정리
    prune_dead_jobs_from_queue(orders)

    # 3. 새 주문 감지 - 픽업(tray_reached/board_mismatched)은 큐잉만,
    #    컨베이어(keycap_packed/board_packed)는 즉시 반응(status 기반 dedup 유지)
    for order_id, order in list(orders.items()):
        if order.get("fault") is not None:
            handled_status.pop(order_id, None)
            continue

        status = order.get("status")

        if status in (STATUS_PICK, STATUS_MISMATCH):
            color = order.get("board")
            retry = order.get("retry")  # 재출고 횟수 - 명세에 필드명 미확정, 없으면 None
            job_key = build_job_key(order_id, color, status, retry)
            if not already_processed(job_key):
                mark_processed(job_key)
                job_queue.append({"order_id": order_id, "color": color})
                enqueue_to_server(order_id, color)
                log("QUEUE", f"{order_id}({color}, {status}, retry={retry}) 큐 적재 - 대기 {len(job_queue)}건")

        elif status == STATUS_CONVEYOR_1:
            if handled_status.get(order_id) != status:
                handle_tray_conveyor(loop, box_api, order_id, "1")
                handled_status[order_id] = status

        elif status == STATUS_CONVEYOR_2:
            if handled_status.get(order_id) != status:
                handle_tray_conveyor(loop, box_api, order_id, "2")
                handled_status[order_id] = status

    for order_id in list(handled_status):
        if order_id not in orders:
            handled_status.pop(order_id, None)


# ============================== 메인 ==============================
def main():
    global local_stock, job_queue, gate_open, gate_order_id

    local_stock = fetch_initial_stock()

    # 서버(Redis)에서 큐/게이트 복원 - 재부팅 대비
    restored_jobs = fetch_queue_from_server()
    job_queue = restored_jobs
    # 복원된 job의 원래 jobKey(status/retry 포함)는 서버 큐에 안 남아있어서
    # 여기선 order_id+color 조합만 알 수 있음 - 재부팅 직후 딱 한 번, 정확히
    # 같은 status+retry로 재전송되는 극히 좁은 경우만 중복 적재될 수 있는
    # 트레이드오프로 남겨둠 (재고/게이트 안전성엔 영향 없음, 최악이어도 같은
    # job이 큐에 두 번 들어가는 정도)

    gate_state = fetch_gate_from_server()
    if gate_state.get("locked"):
        gate_open = False
        gate_order_id = gate_state.get("order_id", "")
    else:
        gate_open = True
        gate_order_id = ""

    mqtt_client = build_mqtt_client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    # DobotlinkAdapter는 "돌고 있지 않은" 이벤트루프를 요구함
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
    arm_api = getattr(dobotlink, NAMESPACE_ARM)
    box_api = getattr(dobotlink, NAMESPACE_BOX)

    log("INIT", f"연결 시도: {DOBOT_PORT}")
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT)))
    log("INIT", f"연결 결과: {connect_res}")
    if not is_ok(connect_res):
        log("INIT", "연결 실패 - DobotLab 연결 끊겨있는지, DobotLink.exe 켜져있는지 확인")

    log("INIT", f"준비 완료 (픽업:{STATUS_PICK}/{STATUS_MISMATCH}, 컨베이어1:{STATUS_CONVEYOR_1}, "
                f"컨베이어2:{STATUS_CONVEYOR_2}, 큐 {len(job_queue)}건, "
                f"게이트 {'닫힘' if not gate_open else '열림'}) - 대기 시작. Ctrl+C로 종료")

    try:
        while True:
            try:
                data = snapshot_queue.get(timeout=1)
                handle_snapshot(loop, arm_api, box_api, data)
            except queue.Empty:
                pass

            # 게이트 열려있고 큐에 대기 중인 job 있으면 하나 처리
            if gate_open and job_queue:
                job = job_queue.pop(0)
                log("ARM", f"{job['order_id']}: 큐에서 꺼내 처리 시작 (색상={job['color']})")
                process_board_job(loop, arm_api, box_api, job["order_id"], job["color"])
    except KeyboardInterrupt:
        log("SHUTDOWN", "Ctrl+C로 종료")
    finally:
        log("SHUTDOWN", "정리 중")
        try:
            loop.run_until_complete(suck(arm_api, False))
        except Exception as e:
            log("SHUTDOWN", f"흡착 OFF 시도 중 에러(무시 가능): {e}")
        try:
            loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        except Exception as e:
            log("SHUTDOWN", f"연결 종료 중 에러(무시 가능): {e}")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()