# -*- coding: utf-8 -*-
"""
=====================================================================
 보드 로봇암 + 트레이 컨베이어 - DobotRPC 단일 + pydobot 스타일 이동
 + 카메라 배치 검증 + fault 재시도 + restock
=====================================================================
⚠️ pydobot(시리얼 직결)과 DobotRPC(DobotLink 경유)는 같은 COM 포트를
   동시에 못 염(포트는 한 프로그램만 독점). 팔/컨베이어가 실제로 같은
   포트라서, 라이브러리를 진짜로 섞어 쓸 수는 없음 - 전부 DobotRPC로
   통일된 상태 유지.

   대신 "이동 방식"만 pydobot 버전(검증됨, 실물로 잘 됐던 방식)의
   전략을 그대로 가져옴:
     - GetPose로 엄격하게 "도착했는지" 확인하고 재시도하는 방식 (X)
       -> DobotRPC의 GetPose도 연속 호출 시 불안정한 게 실물로 확인됨
     - 이동 명령 보내고 고정 시간만큼 대기, 그 다음 위치는 "육안 확인용"
       으로만 한 번 조회 (O)

   대신 "실제로 트레이에 기대한 색 보드가 놓였는지"는 별개 USB 웹캠으로
   확인함 - 이게 지금 유일한 물리적 결과 검증 수단 (도착 좌표 자체는
   여전히 엄격 검증 안 함).

흡착 파라미터: 가이드북 API 레퍼런스로 확정된 대로 "on" 사용
(m_lite.set_endeffector_suctioncup(enable, on)).

주문 로직 - 키캡 디스펜서 아두이노(alreadyProcessed / jobQueue / 서버큐 /
pickup 게이트) 패턴과 동일하게 맞춤:
  - 중복방지: jobKey(order_id+color+status+retry) 최근 8개를 링버퍼로 기억.
    ⚠️ status를 포함한 이유: tray_reached(최초)와 board_mismatched(재출고)가
    같은 order_id+color를 쓰기 때문. retry까지 포함한 이유는 재출고가 최대
    2회 반복될 수 있어서(명세 8-2) - retry 필드가 실제로 오는지는 미확인.
  - 로컬 큐 + 서버 큐(Redis, /queue/board/*) 동기화: tray_reached/
    board_mismatched 감지 시 즉시 처리하지 않고 큐에 적재 -> 재부팅해도
    GET /queue/board/next로 미완료 주문 복원 가능
  - 유령주문 정리: 큐에 남은 job의 order_id가 스냅샷 orders에서 사라지면
    로컬+서버 큐 양쪽에서 제거
  - 픽업 게이트(안전망): 벨트가 주문 1건만 태우므로 정상 흐름에선 항상
    열려있는 상태로 즉시 통과함. 수동 테스트 대비 안전장치로 유지.
    픽업+컨베이어 직후(성공/실패 무관) 잠그고, pickup_reached 이후 상태가
    되거나 목록에서 사라지면 열림 (/gate/board/*)
    ⚠️ GATE_OPEN_STATUSES 철자/unclaimed 포함 여부 플랫폼팀 확인 필요.

fault 처리 (신규) - 우리가 관리하는 fault 3종만:
    board_pack_failed / tray_01_belt_jammed / tray_02_belt_jammed
  흐름: 우리가 failed 보고 -> 플랫폼이 fault 채워서 push (status는 그대로
  유지) -> 사람이 물리적으로 해결 -> 관리자페이지에서 fault:null로 push
  -> 우리가 그 전이(있었다가 없어짐)를 감지 -> 같은 동작 재시도 -> done/failed
  다시 보고.
  ⚠️ 재시도는 큐/게이트를 거치지 않고 직접 실행함 - 이미 게이트를 걸고
  있는 주문 본인이 재시도하는 거라 줄 설 필요가 없고, 큐를 거치면
  "게이트가 열려야 큐 처리하는데 게이트는 이 주문 성공해야 열림"이라는
  데드락이 생기기 때문.
  ⚠️ fault 추적은 로컬 변수만 씀(Redis 영속화 안 함) - fault 정보의
  원본(source of truth)이 우리 메모리가 아니라 매번 오는 MQTT 스냅샷
  자체라서, 재부팅으로 추적 상태를 잃어도 다음 스냅샷에서 "새로 발견"으로
  자동 복구됨 (게이트와 달리 우리 판단이 원본이 아니라서 영속화 불필요).
  ⚠️ 컨베이어 jam은 아직 센서로 자체 감지 안 함(추후 section1/2에 센서
  달아서 체크 예정) - 지금은 플랫폼이 걸어주는 fault에만 반응.

restock 처리 (신규):
  관리자페이지 버튼 -> 플랫폼이 cnt_process 최상위(orders 밖)에
  board_restock: ["blue", "red"] 형태로 push (물리적으로 이미 다음 스택
  교체가 끝났다는 확인 신호로 간주) -> 우리가 감지해서 색상마다
  POST /restock/board/<color> 호출 -> Flask가 pool에서 실제로 차감하며
  채우고 그 결과(filled)를 반환 -> local_stock 갱신.
  같은 색이 여러 스냅샷에 걸쳐 반복 등장해도 한 번만 처리(handled_restock),
  배열에서 사라지면 추적 해제해서 나중에 또 restock 떠도 재처리 가능.

전체 흐름
  [보드 픽업 - tray_reached / board_mismatched(재출고)]
    0. jobKey 중복체크 -> 로컬 큐 + 서버 큐 적재 (게이트 열렸을 때만 실제 처리)
    1. cnt_board_status/cnt_{색} = busy
    2. 배출 (A안 높이계산)
    3. 카메라로 배치 확인 (색상+존재 여부)
    4. cnt_robot_arm/cnt_board_robot_arm = done/failed
    5. 성공 시 cnt_warehouse/cnt_board_dispense
    6. cnt_board_status/cnt_{색} = idle + count-1  -> 플랫폼이 board_packed로 전이
    7. 게이트 잠금 + 서버 큐에서 제거
  [트레이 컨베이어 - keycap_packed(section1) / board_packed(section2)]
    8. 컨베이어(Magic Box) 해당 section 구간 구동 (게이트/큐 없음 - 직접 반응)
    9. cnt_conveyor/cnt_tray_conveyor = {"order_id":..., "sectionN": "done"|"failed"}
  [fault 해제 재시도] - 위 픽업/컨베이어 실패 후 fault 걸렸다가 null 되면
    해당 동작만 큐/게이트 없이 즉시 재실행
  [restock] - board_restock 배열 감지 시 즉시 /restock/board/<color> 호출

⚠️ 미확인 설계 질문 (플랫폼팀/이사님 확인 필요, 코드는 일단 "비어있다"고 가정):
  board_mismatched(재출고) 시점에 트레이 위에 이미 잘못된 보드가 놓여있는
  상태인지 확인 안 됨 - 지금은 트레이 자리가 비어있다는 전제로 그냥 다시 놓음.

실행 전 확인
  - DobotLab 연결 끊어둘 것 (COM 포트 충돌 방지)
  - DobotLink.exe 실행 중이어야 함
  - .env에 MQTT_HOST/PORT/USER/PASS, MQTT_TOPIC, FLASK_BASE_URL
  - pip install DobotRPC opencv-python numpy (Python 3.9 환경 - venv-dobot)
  - calibrate_camera.py로 CAMERA_ROI/COLOR_RANGES_HSV 먼저 확정할 것
=====================================================================
"""

import asyncio
import json
import math
import os
import queue
import time

import cv2
import numpy as np
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

FAULT_RETRY_MAP = {
    "board_pack_failed": "pick",
    "tray_01_belt_jammed": "conveyor1",
    "tray_02_belt_jammed": "conveyor2",
}
STAGE_TIMEOUT_FAULT = "stage_timeout"
TRACKED_FAULTS = set(FAULT_RETRY_MAP) | {STAGE_TIMEOUT_FAULT}

DOBOT_PORT = "COM17"   # 팔 + 컨베이어(Magic Box) 공유 포트
NAMESPACE_ARM = "MagicianLite"
NAMESPACE_BOX = "MagicBox"

# =================================================================
# CONFIG - 로봇암 좌표
# ⚠️ 2026-09-18 실측 갱신(1차): PICK_POSE_BASE의 X/Z/R을 4색 공통값으로 통일.
#    X:290.0 / Z:-35.0(count=1 기준) / R:0.0 통일. Y는 색상별 실측값 유지.
# ⚠️ 2026-09-18 재실측(2차): 보드 3개 적재(count=3) 기준으로 재측정.
#    - X: 이번엔 통일 안 함 - 색상별 실측값 그대로 사용
#    - Y: 항상 색상별 실측값 유지 (컨베이어 위 레일 위치라 통일 대상 아님)
#    - Z: 실측값은 count=3 기준이라 (3-1)*BOARD_THICKNESS_MM(=40mm)를 빼서
#      count=1 기준 base_z로 환산 후, 4색 평균(-33.27)으로 통일
#      (red -33.4577 / green -33.9360 / blue -32.4397 / yellow -33.2272 평균)
#    - R: 0.0 통일 폐기 - 이번엔 실측값을 그대로 사용(회전축이라 적재 개수와 무관)
#    PLACE_POSE는 재실측 반영, CAMERA_CHECK_POSE는 로봇암 장착 카메라용 재실측 위치
#    (2026-09-18, 이동시간 부족으로 도착 못 하고 지나치던 문제 확인 후
#    CAMERA_MOVE_WAIT_SEC 4.0초로 별도 분리 - 아래 이동 타이밍 CONFIG 참고).
# =================================================================
PICK_POSE_BASE = {
    "red":    (215.5975, 134.2798, -37.27, 31.9158),
    "green":  (249.1647,  48.9685, -37.27, 11.1187),
    "blue":   (248.0774, -36.6399, -37.27, -8.4016),
    "yellow": (205.0976, -132.5483, -37.27, -32.8733),
}
BOARD_THICKNESS_MM = 20.0

PLACE_POSE = (-152.0347, -263.0365, 0.4450, -120.0278)
CAMERA_CHECK_POSE = (-122.3321, -200.4390, 58.4218, -121.3965)
HOME_POSE = (240.0, 0.0, 150.0, 0.0)

# =================================================================
# CONFIG - 트레이 컨베이어
# 물리적으로 벨트 2개(STP1/STP2)를 이어붙여 "하나의 긴 벨트"로 씀 - 이 두 모터는
# section1이든 section2든 항상 같이 구동/정지함.
#   section1: keycap_packed 감지 시 구동 - 보드 로봇암 앞까지 이동
#   section2: board_packed  감지 시 구동 - 픽업대까지 이동
# =================================================================
TRAY_CONVEYOR_STP_1 = 0   # Magic Box STP1 포트
TRAY_CONVEYOR_STP_2 = 1   # Magic Box STP2 포트 (벨트 2개 실배선 반영, 2026-09-18)
TRAY_CONVEYOR_SPEED_MM_S = 50

TRAY_CONVEYOR_RUN_TIME_SECTION1 = 11.0   # keycap_packed -> 보드 로봇암 앞까지
TRAY_CONVEYOR_RUN_TIME_SECTION2 = 6.5   # board_packed -> 픽업대까지

# =================================================================
# CONFIG - 이동 타이밍 (pydobot 버전 검증값 참고)
# =================================================================
MOVE_WAIT_SEC = 2.0          # 대부분의 이동에 쓰는 기본 대기시간
PRECISION_MOVE_WAIT_SEC = 3.0  # 정밀 접촉이 필요한 이동(픽업/놓기) 전용 - 짧으면 흡착 실패 실물로 확인됨
CAMERA_MOVE_WAIT_SEC = 4.0    # 카메라 체크 위치 이동 전용 - place_pose -> CAMERA_CHECK_POSE 이동거리(~107mm)가
                              # 다른 구간보다 커서 MOVE_WAIT_SEC(2.0초)로는 도착 전에 다음 명령이 덮어써 그냥
                              # 지나쳐버리는 문제 확인됨(2026-09-18) - 정확히 도착해야 사진이 의미 있어서 넉넉하게 설정
HOME_MOVE_WAIT_SEC = 5.0      # 카메라 체크 위치 -> HOME 복귀 전용 - XY 이동거리(~380mm)에 R 회전(~121도)까지
                              # 겹쳐서 MOVE_WAIT_SEC(2.0초)로는 부족해 복귀가 안 되던 문제 확인됨(2026-09-18)
SUCTION_SETTLE_DELAY = 1.0   # 흡착력 형성 대기
RELEASE_SETTLE_DELAY = 0.5
SAFE_LIFT_MM = 40.0

BOARD_COLORS = ["red", "yellow", "blue", "green"]
CALL_TIMEOUT = 5

# =================================================================
# CONFIG - 카메라 배치 확인
# ⚠️ 2026-09-18 변경: 카메라를 로봇암에 직접 장착하는 방식으로 변경됨.
#    이전(별개 고정 USB 웹캠, cv2로 직접 제어, HOME_POSE 복귀 후 확인)과 달리
#    이제는 보드를 놓은 직후 CAMERA_CHECK_POSE로 이동해서 그 자리에서 확인.
#    장치 접근 방식(cv2.VideoCapture)과 CAMERA_INDEX 자체는 그대로 유지 -
#    카메라 장치가 바뀐 게 아니라 장착 위치만 팔로 옮겨간 것으로 간주.
# ⚠️ CAMERA_ROI 재보정 필요 - 카메라 위치가 완전히 바뀌었는데 아래 값은
#    기존(고정 웹캠 기준) 값 그대로 남겨둔 상태. CAMERA_CHECK_POSE에서 실물로
#    calibrate_camera.py 다시 돌려서 새 ROI 값으로 교체할 것 - 재보정 전까지는
#    색상 판정이 엉뚱한 영역을 보고 있을 수 있음.
# =================================================================
CAMERA_INDEX = 1   # 노트북 내장캠이 0을 먼저 차지하는 경우가 많아 외장캠은 보통 1
CAMERA_ROI = (200, 150, 150, 100)  # (x, y, w, h) - ⚠️ 재보정 필요, 위 주석 참고
CAMERA_PRESENCE_MIN_RATIO = 0.3
CAMERA_SETTLE_DELAY = 0.5   # 사진 찍기 전 대기 - 5초는 과했음(이전 해석 오류), 짧게 줄임

# 실측 완료 (2026-09 촬영) - H:S:V 순서로 yellow(33,59,252) blue(94,68,243)
# green(53,46,240) red/핑크(160,70,255). 전부 채도(S) 낮고 명도(V) 높은
# 파스텔톤이라 S/V는 4색 공통으로 넉넉하게 낮춰 잡음. 노랑/초록 hue가
# 33/53으로 가까워서 중간값(43) 기준으로 여유 없이 나눔 - 오판정 나면
# 여기 경계값부터 조정할 것.
COLOR_RANGES_HSV = {
    "yellow": [((20, 30, 150), (42, 255, 255))],
    "green":  [((44, 30, 150), (70, 255, 255))],
    "blue":   [((75, 30, 150), (120, 255, 255))],
    "red":    [((140, 30, 150), (180, 255, 255))],  # 실측은 핑크(H160)
}

# =================================================================

_MM_TO_PULSE = (360.0 / 1.8 * 5.0 * 16.0) / (3.1415926535898 * 32)


def mm_per_sec_to_pulses(speed_mm_s):
    return int(math.floor(speed_mm_s * _MM_TO_PULSE))


snapshot_queue: "queue.Queue" = queue.Queue()
last_seq = -1
local_stock = {}
handled_status = {}   # (order_id -> status) - 컨베이어(keycap_packed/board_packed) 트리거 전용 dedup

# ---- 픽업 job 중복방지 (키캡 alreadyProcessed 패턴) ----
JOB_HISTORY_SIZE = 8
processed_job_keys = []


def already_processed(job_key):
    return job_key in processed_job_keys


def mark_processed(job_key):
    processed_job_keys.append(job_key)
    if len(processed_job_keys) > JOB_HISTORY_SIZE:
        processed_job_keys.pop(0)


def build_job_key(order_id, color, status, retry):
    return f"{order_id}_{color}_{status}_{retry}"


# ---- 픽업 로컬 큐 (서버 큐와 동기화) ----
job_queue = []

# ---- 픽업 게이트 (안전망) ----
gate_open = True
gate_order_id = ""

GATE_OPEN_STATUSES = (
    "pickup_reached", "loaded", "arrived", "unclaimed", "verified", "received", "completed"
)

# ---- fault 추적 (로컬 변수만 - Redis 영속화 불필요, 이유는 파일 상단 docstring 참고) ----
current_fault_order_id = ""
current_fault_value = ""

# ---- restock 중복방지 (board_restock 배열에 같은 색이 여러 스냅샷 걸쳐 남아있어도 한 번만) ----
handled_restock = set()


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


def call_restock(color):
    """/restock/board/<color> 호출 - Flask가 pool에서 실제로 차감하며 채우고 filled 반환."""
    try:
        resp = requests.post(f"{FLASK_BASE_URL}/restock/board/{color}", timeout=REPORT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        filled = data.get("filled")
        log("STOCK", f"restock/{color} 성공: {filled}개로 채움")
        return filled
    except Exception as e:
        log("STOCK", f"restock/{color} 요청 실패: {e}")
        return None


# ---- 픽업 큐 서버 동기화 ----
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


# ---- 픽업 게이트 서버 동기화 ----
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


# ============================== 이동 - pydobot 스타일 ==============================
async def move_and_wait(arm_api, pose, label="", wait_sec=None):
    if wait_sec is None:
        wait_sec = MOVE_WAIT_SEC
    x, y, z, r = pose
    moved = await safe_call(arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0,
                                               x=x, y=y, z=z, r=r, isQueued=False))
    await asyncio.sleep(wait_sec)

    actual = await safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False))
    log("ARM", f"{label} - 명령:{moved}, 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}), 실제위치:{actual}")
    return is_ok(moved)


async def suck(arm_api, on):
    res = await safe_call(arm_api.SetEndEffectorSuctionCup(
        portName=DOBOT_PORT, enable=True, on=on, isQueued=False
    ))
    log("ARM", f"흡착 {'ON' if on else 'OFF'} 결과: {res}")
    return res


# ============================== 카메라 배치 확인 ==============================
def _capture_frame():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        return None
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _classify_roi_color(frame):
    x, y, w, h = CAMERA_ROI
    roi = frame[y:y + h, x:x + w]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    total_px = roi.shape[0] * roi.shape[1]

    best_color, best_ratio = None, 0.0
    for color, ranges in COLOR_RANGES_HSV.items():
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo), np.array(hi))
        ratio = cv2.countNonZero(mask) / total_px
        if ratio > best_ratio:
            best_ratio, best_color = ratio, color
    return best_color, best_ratio


def verify_board_on_tray(expected_color):
    frame = _capture_frame()
    if frame is None:
        return False, None, "카메라 프레임 획득 실패"

    detected_color, ratio = _classify_roi_color(frame)

    if ratio < CAMERA_PRESENCE_MIN_RATIO:
        return False, detected_color, f"보드 미감지 (비율 {ratio:.2f} < {CAMERA_PRESENCE_MIN_RATIO})"

    if detected_color != expected_color:
        return False, detected_color, f"색상 불일치 - 기대:{expected_color}, 감지:{detected_color}"

    return True, detected_color, f"확인됨 (비율 {ratio:.2f})"


# ============================== 픽업 로직 ==============================
def compute_pick_pose(color, count):
    x, y, base_z, r = PICK_POSE_BASE[color]
    pick_z = base_z + (count - 1) * BOARD_THICKNESS_MM
    return (x, y, pick_z, r)


async def pick_and_place_board_async(arm_api, color, count):
    """
    픽업 -> 안전 높이로 들어올림 -> 안전 높이 유지한 채 이동 -> 트레이로 하강 -> 놓기
    -> 다시 안전 높이로 들어올림 -> 카메라로 배치 확인 -> 초기위치 복귀.
    ⚠️ 이동 자체(도착 좌표)는 pydobot 트레이드오프 그대로 엄격 검증 안 함.
    카메라 확인만이 유일한 물리적 결과 검증 수단.
    """
    pick_x, pick_y, pick_z, pick_r = compute_pick_pose(color, count)
    place_x, place_y, place_z, place_r = PLACE_POSE
    travel_z = max(pick_z, place_z) + SAFE_LIFT_MM
    log("ARM", f"[{color}] 픽업 좌표: ({pick_x:.1f},{pick_y:.1f},{pick_z:.1f}) / 이동 높이: {travel_z:.1f}")

    try:
        await move_and_wait(arm_api, (pick_x, pick_y, pick_z, pick_r), label="픽업 위치로 이동",
                             wait_sec=PRECISION_MOVE_WAIT_SEC)

        grabbed = await suck(arm_api, True)
        await asyncio.sleep(SUCTION_SETTLE_DELAY)

        await move_and_wait(arm_api, (pick_x, pick_y, travel_z, pick_r), label="안전 높이로 들어올리기")
        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r), label="안전 높이 유지한 채 트레이 위로 이동")
        await move_and_wait(arm_api, (place_x, place_y, place_z, place_r), label="트레이 위치로 하강",
                             wait_sec=PRECISION_MOVE_WAIT_SEC)

        released = await suck(arm_api, False)
        await asyncio.sleep(RELEASE_SETTLE_DELAY)

        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r), label="다시 안전 높이로 들어올리기")

        # ⚠️ 2026-09-18 변경: 카메라가 로봇암에 장착돼서, 보드를 놓은 직후
        # CAMERA_CHECK_POSE로 먼저 이동해서 그 자리에서 확인 - HOME 복귀는
        # 확인 이후로 미룸(이전엔 순서가 반대: HOME 복귀 후 고정 웹캠으로 확인).
        await move_and_wait(arm_api, CAMERA_CHECK_POSE, label="카메라 체크 위치로 이동",
                             wait_sec=CAMERA_MOVE_WAIT_SEC)

        await asyncio.sleep(CAMERA_SETTLE_DELAY)
        log("ARM", "카메라로 배치 확인 중")
        verified, detected_color, reason = verify_board_on_tray(color)
        log("ARM", f"카메라 확인 결과: {reason} (감지된 색={detected_color})")

        # ⚠️ 2026-09-18: CAMERA_CHECK_POSE -> HOME_POSE 구간 R 회전폭이 커서
        # (약 121도) "R+XY 동시 이동 씹힘" 의심했었으나, 실제 원인은 대기시간
        # 부족이었음 확인됨 - 2단계 분리 대신 HOME_MOVE_WAIT_SEC로 충분히 대기.
        await move_and_wait(arm_api, HOME_POSE, label="초기위치로 복귀",
                             wait_sec=HOME_MOVE_WAIT_SEC)

        if not verified:
            log("ARM", "카메라 확인 실패 - 실패 처리 (물리 동작은 완료했지만 done 보고 안 함)")
            return False

        log("ARM", "결과: 성공 (카메라로 배치 확인됨)")
        return True

    except Exception as e:
        log("ARM", f"예외 발생 - 실패 처리: {e}")
        try:
            await suck(arm_api, False)
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
    큐에서 꺼낸 job 1건 처리 (또는 fault 해제 후 직접 재시도).
    픽업 실행 후, 성공/실패 무관하게 게이트 잠금 + 서버 큐 제거.
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


# ---- 게이트: 기다리던 주문이 pickup_reached(이후) 됐는지 확인 ----
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


# ---- fault 추적: 전이 감지 시 재시도 ----
def check_fault_transitions(loop, arm_api, box_api, orders):
    global current_fault_order_id, current_fault_value

    if current_fault_order_id:
        order = orders.get(current_fault_order_id)

        if order is None:
            current_fault_order_id, current_fault_value = "", ""
            return

        if order.get("fault") is None:
            oid, fval = current_fault_order_id, current_fault_value
            current_fault_order_id, current_fault_value = "", ""
            log("FAULT", f"{oid}: {fval} 해제")

            if fval == STAGE_TIMEOUT_FAULT:
                handle_stage_timeout_retry(oid, order)
                return

            retry_kind = FAULT_RETRY_MAP.get(fval)
            color = order.get("board")
            if retry_kind == "pick":
                process_board_job(loop, arm_api, box_api, oid, color)
            elif retry_kind == "conveyor1":
                handle_tray_conveyor(loop, box_api, oid, "1")
            elif retry_kind == "conveyor2":
                handle_tray_conveyor(loop, box_api, oid, "2")
        return

    for order_id, order in orders.items():
        fault = order.get("fault")
        if fault in TRACKED_FAULTS:
            current_fault_order_id = order_id
            current_fault_value = fault
            log("FAULT", f"{order_id}: {fault} 감지")
            break


def handle_stage_timeout_retry(order_id, order):
    status = order.get("status")
    color = order.get("board")

    if status in (STATUS_PICK, STATUS_MISMATCH):
        job_key = build_job_key(order_id, color, status, order.get("retry"))
        if not already_processed(job_key):
            mark_processed(job_key)
            job_queue.append({"order_id": order_id, "color": color})
            enqueue_to_server(order_id, color)
            log("QUEUE", f"{order_id}({color}) stage_timeout -> 큐 적재")


# ---- 재부팅 시 fault 해제 전이를 놓친 경우 복구 ----
startup_recovery_checked = False


def check_startup_recovery(loop, arm_api, box_api, orders):
    global startup_recovery_checked

    if startup_recovery_checked:
        return
    startup_recovery_checked = True

    if gate_open or not gate_order_id:
        return

    order = orders.get(gate_order_id)
    if order is None or order.get("fault") is not None:
        return

    status = order.get("status")
    if status in (STATUS_PICK, STATUS_MISMATCH):
        color = order.get("board")
        log("GATE", f"재부팅 복구: {gate_order_id} 재시도")
        process_board_job(loop, arm_api, box_api, gate_order_id, color)


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


# ---- restock: board_restock 배열 감지해서 즉시 처리 (큐/게이트 무관) ----
def check_board_restock(data):
    board_restock = [c for c in (data.get("board_restock") or []) if c in BOARD_COLORS]

    for color in board_restock:
        if color not in handled_restock:
            handled_restock.add(color)
            filled = call_restock(color)
            if filled is not None:
                local_stock[color] = filled
                log("STOCK", f"[{color}] restock 처리 완료 -> {filled}개로 채움")
            else:
                handled_restock.discard(color)  # 실패했으면 다음 스냅샷에 재시도 가능하게 원복

    # 배열에서 사라진 색은 추적 해제 - 나중에 다시 떠도(재소진) 재처리 가능하게
    for color in list(handled_restock):
        if color not in board_restock:
            handled_restock.discard(color)


def handle_snapshot(loop, arm_api, box_api, data):
    orders = data.get("orders", {})

    # 1. 게이트 재평가
    check_gate_against_orders(orders)

    # 1-1. 재부팅 복구 (세션 시작 후 딱 한 번만 실행됨 - 함수 내부에서 자체 가드)
    check_startup_recovery(loop, arm_api, box_api, orders)

    # 2. fault 해제 감지 -> 재시도 (큐/게이트 무관, 직접 실행)
    check_fault_transitions(loop, arm_api, box_api, orders)

    # 3. restock 감지 -> 즉시 처리 (큐/게이트 무관)
    check_board_restock(data)

    # 4. 유령주문 정리
    prune_dead_jobs_from_queue(orders)

    # 5. 새 주문 감지 - 픽업(tray_reached/board_mismatched)은 큐잉만,
    #    컨베이어(keycap_packed/board_packed)는 즉시 반응(status 기반 dedup 유지)
    for order_id, order in list(orders.items()):
        if order.get("fault") is not None:
            handled_status.pop(order_id, None)
            continue

        status = order.get("status")

        if status in (STATUS_PICK, STATUS_MISMATCH):
            color = order.get("board")
            retry = order.get("retry")
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

    restored_jobs = fetch_queue_from_server()
    job_queue = restored_jobs
    for job in restored_jobs:
        mark_processed(f"{job['order_id']}_{job['color']}")

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