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

   이동 방식: 이동 명령 보낸 뒤 GetPose를 MOVE_POLL_INTERVAL_SEC 간격으로
   조회하며 목표 도착(허용오차 이내) + 멈춤까지 대기, 목표 못 간 채 멈춰
   있으면 명령 재전송, MOVE_TIMEOUT_SEC 초과 시 도착 실패로 봄
   (move_and_wait). 픽업 위치 / 트레이 하강은 도착 실패 시 failed 처리,
   나머지 이동은 로그만 남기고 진행.
     ⚠️ 예전엔 "DobotRPC의 GetPose 연속 호출이 불안정"해서 고정 시간 대기
       방식을 썼음 - GetPose가 TIMEOUT/ERROR를 반복하면 이동마다
       MOVE_TIMEOUT_SEC를 다 기다리게 되므로 로그의 "도착 실패" 확인할 것.

   "실제로 트레이에 기대한 색 보드가 놓였는지"는 별개 USB 웹캠으로
   확인함 - 최종 결과 검증 수단.

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
    GATE_OPEN_STATUSES 철자/unclaimed 포함 - 플랫폼 문서(Mobius CNT·CIN 구조 정리,
    2026-10-02) 2-2 status 표와 일치 확인됨.

fault 처리 - 우리가 관리하는 fault 3종만:
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
  ⚠️ 2026-09-18: stage_timeout은 추적/재시도 대상이 아님 - 플랫폼팀 확인
  결과 정해진 시간 초과 시 플랫폼이 주문 자체를 폐기하는 fault라, 우리가
  뭘 재시도할 필요가 없음. 주문 폐기되면 orders에서 사라지고 그건 유령주문
  정리(prune_dead_jobs_from_queue)/게이트 자동 열림(check_gate_against_orders)이
  이미 처리함.

restock 처리 (신규):
  관리자페이지 버튼 -> 플랫폼이 cnt_process 최상위(orders 밖)에
  board_restock: ["b", "r"] 색 코드 형태로 push (물리적으로 이미 다음 스택
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
  - 좌표/컨베이어/카메라/HSV 등 튜닝값은 전부 board_config.py에서 수정
    (CAMERA_ROI/COLOR_RANGES_HSV는 camera.py로 확인 후 board_config.py에 반영)
=====================================================================
"""

import asyncio
import json
import math
import os
import queue
import time
import uuid

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

# Mobius 직접 조회용 (재부팅 복구 때 cnt_board_robot_arm 최신 보고 확인) - 없으면 그 확인만 건너뜀
COSS_BASE_URL = os.getenv("COSS_BASE_URL")
AE_NAME = os.getenv("AE_NAME")
COSS_API_KEY = os.getenv("COSS_API_KEY")
COSS_LECTURE = os.getenv("COSS_LECTURE")
COSS_CREATOR = os.getenv("COSS_CREATOR")
COSS_ORIGIN = os.getenv("ORIGIN")

STATUS_PICK = "tray_reached"           # 보드 픽업 트리거
STATUS_MISMATCH = "board_mismatched"   # 재출고 트리거 - tray_reached와 같은 핸들러로 처리
STATUS_CONVEYOR_1 = "keycap_packed"    # 컨베이어 section1 - 보드 로봇암 앞까지 이동
STATUS_CONVEYOR_2 = "board_packed"     # 컨베이어 section2 - 픽업대까지 이동

FAULT_RETRY_MAP = {
    "board_pack_failed": "pick",
    "tray_01_belt_jammed": "conveyor1",
    "tray_02_belt_jammed": "conveyor2",
}
# ⚠️ 2026-09-18: stage_timeout은 재시도 대상에서 제외 - 플랫폼팀 확인 결과
# stage_timeout은 우리가 재시도할 게 아니라 플랫폼이 정해진 시간 초과 시
# 주문 자체를 폐기하는 fault임. 주문이 폐기되면 스냅샷 orders에서 사라지고,
# 그건 prune_dead_jobs_from_queue()(로컬/서버 큐 정리)와
# check_gate_against_orders()(게이트 - gate_order_id가 orders에서 사라지면
# 자동으로 열림)가 이미 처리하고 있어서 별도 대응 불필요.
TRACKED_FAULTS = set(FAULT_RETRY_MAP)

NAMESPACE_ARM = "MagicianLite"
NAMESPACE_BOX = "MagicBox"
CALL_TIMEOUT = 5

# 튜닝값(좌표/컨베이어/타이밍/카메라/HSV)은 전부 board_config.py에 있음
from board_config import (
    DOBOT_PORT,
    PICK_POSE_ROWS, PLACE_XYZ, PLACE_R_OFFSET_DEG_BY_COLOR, CAMERA_CHECK_XYZ, HOME_XYZ, HOME_WRIST_DEG, SAFE_LIFT_MM,
    MOVE_TIMEOUT_SEC, MOVE_POLL_INTERVAL_SEC, PRECISION_SETTLE_SEC,
    MOVE_STABLE_MM, MOVE_STABLE_DEG, MOVE_RESEND_AFTER_SEC, MOVE_MAX_RESEND,
    SUCTION_SETTLE_DELAY, RELEASE_SETTLE_DELAY,
    POSITION_TOLERANCE_MM, POSITION_TOLERANCE_DEG,
    TRAY_CONVEYOR_STP_1, TRAY_CONVEYOR_STP_2, TRAY_CONVEYOR_SPEED_MM_S,
    TRAY_CONVEYOR_RUN_TIME_SECTION1, TRAY_CONVEYOR_RUN_TIME_SECTION2,
    CAMERA_INDEX_PREFERENCE, CAMERA_ROI, CAMERA_PRESENCE_MIN_RATIO, CAMERA_SETTLE_DELAY,
    CAMERA_WARMUP_FRAMES, SAVE_CAMERA_IMAGE, SHOW_CAMERA_WINDOW, CAMERA_WINDOW_MS,
    CAMERA_IMAGE_DIR, COLOR_RANGES_HSV,
    BOARD_COLORS, compute_pick_pose, wrist_of, r_for, place_r_for, base_angle, arc_midpoint, needs_midpoint, _wrap_deg,
)

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


def complete_job(order_id, color):
    """job 완료/폐기 - 로컬 큐와 서버 큐 양쪽에서 제거.
    (재부팅 복구/fault 재시도처럼 큐를 안 거치고 직접 처리한 job도 로컬 큐에 남지 않게)"""
    global job_queue
    job_queue = [j for j in job_queue if not (j["order_id"] == order_id and j["color"] == color)]
    complete_on_server(order_id, color)


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


# ---- Mobius 직접 조회 ----
def fetch_latest_board_robot_arm():
    """cnt_robot_arm/cnt_board_robot_arm 최신 CIN의 con(dict) - 플랫폼이 board_packed 판단에
    쓰는 원본. .env에 COSS 값이 없거나 조회 실패면 None (판단 근거 없음)."""
    if not all([COSS_BASE_URL, AE_NAME, COSS_API_KEY, COSS_LECTURE, COSS_CREATOR, COSS_ORIGIN]):
        log("MOBIUS", ".env에 COSS 값이 없어 cnt_board_robot_arm 조회 생략")
        return None
    url = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/cnt_robot_arm/cnt_board_robot_arm/la"
    headers = {
        "X-API-KEY": COSS_API_KEY,
        "X-AUTH-CUSTOM-LECTURE": COSS_LECTURE,
        "X-AUTH-CUSTOM-CREATOR": COSS_CREATOR,
        "Accept": "application/json",
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "3",
        "X-M2M-Origin": COSS_ORIGIN,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=REPORT_TIMEOUT)
        if resp.status_code != 200:
            log("MOBIUS", f"cnt_board_robot_arm 최신 CIN 조회 실패 ({resp.status_code})")
            return None
        con = resp.json().get("m2m:cin", {}).get("con")
        for _ in range(2):   # 이중 인코딩 대비 최대 2회 파싱
            if isinstance(con, str):
                con = json.loads(con)
        return con if isinstance(con, dict) else None
    except Exception as e:
        log("MOBIUS", f"cnt_board_robot_arm 조회 에러: {e}")
        return None


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


# ============================== 이동 - 도착 대기 방식 ==============================
async def move_and_wait(arm_api, pose, label="", settle_sec=0.0):
    """
    이동 명령을 보낸 뒤, 목표 위치에 "도착 + 멈춤"할 때까지 GetPose로 확인하며 대기.
    - 허용오차 안이어도 아직 움직이는 중이면 도착으로 안 봄 (그때 다음 명령 보내면 씹힘)
    - 목표 못 간 채 MOVE_RESEND_AFTER_SEC 동안 멈춰 있으면 명령 재전송 (최대 MOVE_MAX_RESEND번),
      재전송 다 써도 안 되면 바로 도착 실패
    - MOVE_TIMEOUT_SEC 안에 도착 못 하면 마지막으로 읽은 위치를 그대로 반환(도착 실패)
    반환: (명령 성공 여부, 마지막 실제 위치)
    """
    x, y, z, r = pose

    def send():
        return safe_call(arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0,
                                           x=x, y=y, z=z, r=r, isQueued=False))

    started = time.time()
    moved = await send()
    actual = None
    arrived = False
    resends = 0
    if is_ok(moved):
        prev = None
        still_since = time.time()
        while time.time() - started < MOVE_TIMEOUT_SEC:
            await asyncio.sleep(MOVE_POLL_INTERVAL_SEC)
            actual = await safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False))
            if not isinstance(actual, dict):
                continue
            if prev is None or not _is_still(prev, actual):
                still_since = time.time()   # 아직 움직이는 중
            elif _position_matches(actual, pose):
                arrived = True
                break
            elif time.time() - still_since >= MOVE_RESEND_AFTER_SEC:
                if resends >= MOVE_MAX_RESEND:
                    break
                resends += 1
                moved = await send()
                log("ARM", f"{label} - 목표 못 간 채 멈춤 -> 명령 재전송 {resends}/{MOVE_MAX_RESEND} ({moved})")
                still_since = time.time()
            prev = actual
    if arrived and settle_sec > 0:
        await asyncio.sleep(settle_sec)

    elapsed = time.time() - started
    status = f"도착 {elapsed:.1f}초" if arrived else f"!! 도착 실패({elapsed:.1f}초)"
    if resends:
        status += f", 재전송 {resends}회"
    log("ARM", f"{label} - 명령:{moved}, {status}, 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}), 실제위치:{actual}")
    return is_ok(moved), actual


def _position_matches(actual, target_pose):
    if not isinstance(actual, dict):
        return False
    tx, ty, tz, tr = target_pose
    try:
        dx = abs(actual.get("x", 0) - tx)
        dy = abs(actual.get("y", 0) - ty)
        dz = abs(actual.get("z", 0) - tz)
        dr = abs(_wrap_deg(actual.get("r", 0) - tr))
    except TypeError:
        return False
    return (dx <= POSITION_TOLERANCE_MM and dy <= POSITION_TOLERANCE_MM
            and dz <= POSITION_TOLERANCE_MM and dr <= POSITION_TOLERANCE_DEG)


def _is_still(prev, cur):
    """직전 조회 대비 거의 안 움직였으면 True (= 팔이 멈춘 상태)."""
    try:
        return (abs(cur["x"] - prev["x"]) <= MOVE_STABLE_MM
                and abs(cur["y"] - prev["y"]) <= MOVE_STABLE_MM
                and abs(cur["z"] - prev["z"]) <= MOVE_STABLE_MM
                and abs(_wrap_deg(cur["r"] - prev["r"])) <= MOVE_STABLE_DEG)
    except (KeyError, TypeError):
        return False


async def suck(arm_api, on):
    res = await safe_call(arm_api.SetEndEffectorSuctionCup(
        portName=DOBOT_PORT, enable=True, on=on, isQueued=False
    ))
    log("ARM", f"흡착 {'ON' if on else 'OFF'} 결과: {res}")
    return res


# ============================== 카메라 배치 확인 ==============================
_resolved_camera_index = None  # 최초 캡처 시 탐색해서 캐싱 - 이후엔 재탐색 안 함


def _open_camera(idx):
    # 윈도우 기본 백엔드(MSMF)는 여는 데 5~30초씩 걸려서 DirectShow로 지정
    return cv2.VideoCapture(idx, cv2.CAP_DSHOW)


def _resolve_camera_index():
    """CAMERA_INDEX_PREFERENCE 순서대로 시도해서 실제로 열리고 프레임까지
    읽히는 첫 인덱스를 찾아 캐싱. USB 재연결마다 장치 인덱스가 바뀌는 문제 대응."""
    global _resolved_camera_index
    if _resolved_camera_index is not None:
        return _resolved_camera_index

    for idx in CAMERA_INDEX_PREFERENCE:
        cap = _open_camera(idx)
        if cap.isOpened():
            ok, _ = cap.read()
            cap.release()
            if ok:
                log("CAM", f"카메라 인덱스 {idx}번으로 확정")
                _resolved_camera_index = idx
                return idx
        cap.release()

    log("CAM", f"카메라 인덱스 탐색 실패 - {CAMERA_INDEX_PREFERENCE} 전부 안 열림")
    return None


def _capture_frame():
    global _resolved_camera_index
    idx = _resolve_camera_index()
    if idx is None:
        return None
    cap = _open_camera(idx)
    if not cap.isOpened():
        # 캐싱된 인덱스가 이번엔 안 열림 (장치 빠졌다 다시 꽂힌 경우 등) - 재탐색
        _resolved_camera_index = None
        idx = _resolve_camera_index()
        if idx is None:
            return None
        cap = _open_camera(idx)
        if not cap.isOpened():
            return None

    # 앞 프레임 몇 장은 자동노출 적응 전이라 어두움 - 버리고 마지막 프레임 사용
    frame = None
    for _ in range(CAMERA_WARMUP_FRAMES + 1):
        ok, f = cap.read()
        if ok:
            frame = f
    cap.release()
    return frame


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


def _draw_roi_box(frame):
    """디버그 저장/표시용 - ROI 사각형만 그린 복사본 반환 (원본 frame은 안 건드림)."""
    boxed = frame.copy()
    x, y, w, h = CAMERA_ROI
    cv2.rectangle(boxed, (x, y), (x + w, y + h), (0, 255, 0), 2)
    return boxed


def _save_camera_image(frame, expected_color):
    try:
        os.makedirs(CAMERA_IMAGE_DIR, exist_ok=True)
        filename = os.path.join(
            CAMERA_IMAGE_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{expected_color}.jpg"
        )
        # ⚠️ cv2.imwrite는 Windows에서 비-ASCII(한글 등) 경로를 못 읽어 조용히
        # 저장 실패하는 경우가 있음(OpenCV 고질 이슈) - imencode+tofile로 우회.
        ok, buf = cv2.imencode(".jpg", _draw_roi_box(frame))
        if not ok:
            log("CAM", "이미지 인코딩 실패")
            return
        buf.tofile(filename)
        log("CAM", f"캡처 이미지 저장: {filename}")
    except Exception as e:
        log("CAM", f"이미지 저장 실패: {e}")


def _show_camera_window(frame):
    try:
        cv2.imshow("board camera check", _draw_roi_box(frame))
        cv2.waitKey(CAMERA_WINDOW_MS)
        cv2.destroyWindow("board camera check")
    except Exception as e:
        log("CAM", f"창 표시 실패(헤드리스 환경일 수 있음): {e}")


def verify_board_on_tray(expected_color):
    frame = _capture_frame()
    if frame is None:
        return False, None, "카메라 프레임 획득 실패"

    if SAVE_CAMERA_IMAGE:
        _save_camera_image(frame, expected_color)
    if SHOW_CAMERA_WINDOW:
        _show_camera_window(frame)

    detected_color, ratio = _classify_roi_color(frame)

    if ratio < CAMERA_PRESENCE_MIN_RATIO:
        return False, detected_color, f"보드 미감지 (비율 {ratio:.2f} < {CAMERA_PRESENCE_MIN_RATIO})"

    if detected_color != expected_color:
        return False, detected_color, f"색상 불일치 - 기대:{expected_color}, 감지:{detected_color}"

    return True, detected_color, f"확인됨 (비율 {ratio:.2f})"


# ============================== 픽업 로직 ==============================
# 손목 각도 고정 규칙: 픽업 R로 손목 각도를 정하고, 이후 모든 위치는 그 손목
# 각도를 유지한 R(r_for)로 이동 - 별도의 "제자리 R 회전" 단계 없음.
# compute_pick_pose / r_for / arc_midpoint 등 계산식은 board_config.py에 있음.

async def travel(arm_api, from_xyz, to_xyz, wrist, label, final_settle=0.0):
    """from -> to 이동. 두 점 베이스 각도 차가 크면(MIDPOINT_MIN_ANGLE_DEG 이상)
    극좌표 중간점을 경유. 중간점 높이는 두 점 중 높은 쪽.
    반환: 마지막 move_and_wait의 (명령성공, 실제위치)."""
    if needs_midpoint(from_xyz[:2], to_xyz[:2]):
        mx, my = arc_midpoint(from_xyz[:2], to_xyz[:2])
        mz = max(from_xyz[2], to_xyz[2])
        await move_and_wait(arm_api, (mx, my, mz, r_for(mx, my, wrist)), label=f"{label} - 중간점 경유")
    tx, ty, tz = to_xyz
    return await move_and_wait(arm_api, (tx, ty, tz, r_for(tx, ty, wrist)), label=label,
                               settle_sec=final_settle)


async def pick_and_place_board_async(arm_api, order_id, color, count):
    """
    HOME -> (중간점) -> 픽업 위 -> 픽업 하강 -> 흡착 -> 들어올림 -> (중간점) ->
    트레이 위 -> 하강 -> 놓기 -> 들어올림 -> 카메라 확인 위치 -> 확인 -> (중간점) -> HOME.

    픽업 위치 / 트레이 하강 두 구간만 GetPose로 도착 확인. 여기서 실패하면 카메라
    확인 없이 즉시 board:"failed" 보고. 성공(board:"done")은 카메라 확인까지 통과해야만.

    반환: (success, consumed)
      consumed = 스택에서 보드를 실제로 집어 올렸는지 - 재고 차감 여부 판단용.
      픽업 위치 도달 실패(흡착 전)만 False, 그 이후 실패는 전부 True.
    """
    consumed = False
    travel_z = None   # 예외 복구(_recover_to_home)에서 참조 - 좌표 계산 전 예외 대비
    try:
        pick_x, pick_y, pick_z, pick_r = compute_pick_pose(color, count)
        wrist = wrist_of(pick_x, pick_y, pick_r)
        place_x, place_y, place_z = PLACE_XYZ
        travel_z = max(pick_z, place_z) + SAFE_LIFT_MM
        log("ARM", f"[{color}] 픽업 좌표: ({pick_x:.1f},{pick_y:.1f},{pick_z:.1f}) R={pick_r:.1f} "
                    f"손목={wrist:.1f} / 이동 높이: {travel_z:.1f}")

        # HOME -> 픽업 위 (안전 높이) -> 픽업 하강
        await travel(arm_api, (HOME_XYZ[0], HOME_XYZ[1], travel_z), (pick_x, pick_y, travel_z),
                     wrist, "픽업 위치 위로 이동")
        pick_target = (pick_x, pick_y, pick_z, r_for(pick_x, pick_y, wrist))
        pick_move_ok, pick_actual = await move_and_wait(arm_api, pick_target, label="픽업 위치로 하강",
                                                         settle_sec=PRECISION_SETTLE_SEC)
        if not pick_move_ok or not _position_matches(pick_actual, pick_target):
            log("ARM", f"픽업 위치 도달 실패 - 목표={tuple(round(v, 1) for v in pick_target)} "
                        f"실제={pick_actual} - 흡착 시도 안 하고 실패 처리")
            report_board_robot_arm(order_id, "failed")
            await travel(arm_api, (pick_x, pick_y, travel_z), HOME_XYZ, wrist, "초기위치로 복귀")
            return False, consumed

        await suck(arm_api, True)
        await asyncio.sleep(SUCTION_SETTLE_DELAY)
        consumed = True   # 이 시점부터는 스택에서 보드가 빠진 것으로 간주

        await move_and_wait(arm_api, (pick_x, pick_y, travel_z, r_for(pick_x, pick_y, wrist)),
                            label="안전 높이로 들어올리기")

        # 픽업 위 -> (중간점) -> 트레이 위 -> 하강
        await travel(arm_api, (pick_x, pick_y, travel_z), (place_x, place_y, travel_z),
                     wrist, "트레이 위로 이동")
        # 트레이 위에서 XY 고정한 채 R만 색별 놓기 R로 회전 (하강 전)
        # 놓기 R = 기준 R(손목 0도) + 색별 회전값 - 픽업 R/줄과 무관하게 색만 같으면 항상 같음
        place_r = place_r_for(color)
        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r),
                            label=f"트레이 위에서 R 회전 ({color} {PLACE_R_OFFSET_DEG_BY_COLOR[color]:+.1f}도, R={place_r:.1f})")
        place_target = (place_x, place_y, place_z, place_r)
        descend_ok, descend_actual = await move_and_wait(arm_api, place_target, label="트레이 위치로 하강",
                                                          settle_sec=PRECISION_SETTLE_SEC)
        if not descend_ok or not _position_matches(descend_actual, place_target):
            log("ARM", f"트레이 하강 도달 실패 - 목표={tuple(round(v, 1) for v in place_target)} "
                        f"실제={descend_actual} - 흡착 끄고 실패 처리")
            report_board_robot_arm(order_id, "failed")
            try:
                await suck(arm_api, False)
            except Exception:
                pass
            await travel(arm_api, (place_x, place_y, travel_z), HOME_XYZ, wrist, "초기위치로 복귀")
            return False, consumed

        await suck(arm_api, False)
        await asyncio.sleep(RELEASE_SETTLE_DELAY)

        await move_and_wait(arm_api, (place_x, place_y, travel_z, place_r),
                            label="다시 안전 높이로 들어올리기")
        # 놓은 뒤엔 손목 각도를 유지할 필요 없음 - 제자리에서 손목 0도로 되돌린 뒤 이동
        # (회전은 제자리에서만 - 이동+큰 회전 동시에 시키면 씹히는 문제 회피)
        await move_and_wait(arm_api, (place_x, place_y, travel_z, r_for(place_x, place_y, HOME_WRIST_DEG)),
                            label="놓은 뒤 R 원위치 (손목 0도)")

        # 카메라 확인 위치로 이동 후 확인 (카메라 위치/HOME은 항상 손목 0도 고정)
        await travel(arm_api, (place_x, place_y, travel_z), CAMERA_CHECK_XYZ, HOME_WRIST_DEG,
                     "카메라 체크 위치로 이동")
        await asyncio.sleep(CAMERA_SETTLE_DELAY)
        log("ARM", "카메라로 배치 확인 중")
        verified, detected_color, reason = verify_board_on_tray(color)
        log("ARM", f"카메라 확인 결과: {reason} (감지된 색={detected_color})")

        # 카메라 위치 -> (중간점) -> HOME
        await travel(arm_api, CAMERA_CHECK_XYZ, HOME_XYZ, HOME_WRIST_DEG, "초기위치로 복귀")

        report_board_robot_arm(order_id, "done" if verified else "failed")

        if not verified:
            log("ARM", "카메라 확인 실패 - 실패 처리")
            return False, consumed

        log("ARM", "결과: 성공 (카메라로 배치 확인됨)")
        return True, consumed

    except Exception as e:
        log("ARM", f"예외 발생 - 실패 처리: {e}")
        try:
            await suck(arm_api, False)
        except Exception:
            pass
        report_board_robot_arm(order_id, "failed")
        await _recover_to_home(arm_api, travel_z)
        return False, consumed


async def _recover_to_home(arm_api, travel_z):
    """예외 후 복귀 - 현재 위치를 모르면 안 움직임. 아는 경우 제자리 수직 상승 후
    현재 손목 각도 유지한 채 HOME으로 (이동 중 큰 회전 방지)."""
    try:
        actual = await safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False))
        if not isinstance(actual, dict):
            log("ARM", f"현재 위치 조회 실패({actual}) - HOME 복귀 생략, 수동 확인 필요")
            return
        cx, cy, cz, cr = actual["x"], actual["y"], actual["z"], actual["r"]
        safe_z = max(cz, travel_z) if travel_z is not None else cz + SAFE_LIFT_MM
        # wrist_of()는 -180~180으로 접어서 R이 360도 차이날 수 있음 - 접지 않은 값 사용
        wrist = cr - base_angle(cx, cy)
        await move_and_wait(arm_api, (cx, cy, safe_z, cr),
                            label="예외 복구 - 제자리 들어올리기")
        await travel(arm_api, (cx, cy, safe_z), HOME_XYZ, wrist, "예외 복구 - 초기위치로 복귀")
    except Exception as e:
        log("ARM", f"예외 복구 중 에러 - HOME 복귀 실패, 수동 확인 필요: {e}")


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
    픽업 실행 후, 성공/실패 무관하게 게이트 잠금 + 로컬/서버 큐 제거.
    """
    global gate_open, gate_order_id

    if color not in BOARD_COLORS or color not in PICK_POSE_ROWS:
        log("ARM", f"알 수 없는 색상 '{color}' - 중단")
        complete_job(order_id, color)
        return

    count_before = local_stock.get(color, 0)
    if count_before <= 0:
        log("ARM", f"[{color}] 재고 0 - 픽업 불가")
        report_board_status(order_id, color, "empty", count=0)
        report_board_robot_arm(order_id, "failed")
    else:
        report_board_status(order_id, color, "busy", count=count_before)
        # board 필드 보고는 pick_and_place_board_async 내부에서 정확한 시점에
        # 이뤄짐 (위치검증 실패 시 즉시, 성공 시 카메라 확인 후)
        success, consumed = loop.run_until_complete(
            pick_and_place_board_async(arm_api, order_id, color, count_before))

        if success:
            report_board_dispense(order_id, color)

        # 재고: 스택에서 실제로 집어 올렸으면(consumed) 성공/실패 무관하게 -1.
        # 픽업 위치 도달 실패(흡착 전)만 그대로 유지.
        if consumed:
            count_after = count_before - 1
            local_stock[color] = count_after
            log("STOCK", f"[{color}] 재고 {count_before} -> {count_after}"
                         f"{'' if success else ' (실패했지만 스택에서 빠졌으므로 차감)'}")
        else:
            count_after = count_before
        report_board_status(order_id, color, "idle", count=count_after)

    gate_open = False
    gate_order_id = order_id
    lock_gate_on_server(order_id)
    log("GATE", f"잠금: {order_id} (pickup_reached 이후 대기)")
    complete_job(order_id, color)


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
        # 처리함 표시 - 안 하면 같은 스냅샷의 새 주문 감지(handle_snapshot 5단계)가
        # 아직 tray_reached인 이 주문을 또 큐에 넣어서 게이트 열릴 때 중복 픽업함
        mark_processed(build_job_key(gate_order_id, color, status, order.get("retry")))

        # done 보고 후 플랫폼이 board_packed로 넘기기 전에 재부팅된 경우 - 이미 성공한
        # 주문이라 다시 집으면 안 됨. tray_reached일 때만 (board_mismatched는 done 보고
        # 후에도 재투입이 필요한 상태라 그대로 재시도)
        if status == STATUS_PICK:
            last = fetch_latest_board_robot_arm()
            if last and last.get("order_id") == gate_order_id and last.get("board") == "done":
                log("GATE", f"재부팅 복구: {gate_order_id} done 보고 확인됨 - 재시도 안 함, 플랫폼 전이 대기")
                return

        log("GATE", f"재부팅 복구: {gate_order_id} 재시도")
        process_board_job(loop, arm_api, box_api, gate_order_id, color)


# ---- 큐에 남은 job 중 유령주문 정리 ----
# orders에서 사라진 주문 + 주문의 board 색과 다른 job(주문 1건 = 보드 1색이라 찌꺼기)
def prune_dead_jobs_from_queue(orders):
    global job_queue
    alive, removed, mismatched = [], 0, 0
    for job in job_queue:
        order = orders.get(job["order_id"])
        if order is None:
            complete_on_server(job["order_id"], job["color"])
            removed += 1
        elif order.get("board") is not None and order.get("board") != job["color"]:
            log("QUEUE", f"{job['order_id']}: 큐의 색({job['color']})이 주문 색({order.get('board')})과 다름 - 제거")
            complete_on_server(job["order_id"], job["color"])
            mismatched += 1
        else:
            alive.append(job)
    job_queue = alive
    if removed:
        log("QUEUE", f"orders에서 사라진 job {removed}건 제거함")
    if mismatched:
        log("QUEUE", f"주문 색과 다른 job {mismatched}건 제거함")


# ---- restock: board_restock 배열 감지해서 즉시 처리 (큐/게이트 무관) ----
BOARD_CODE_TO_COLOR = {"r": "red", "y": "yellow", "g": "green", "b": "blue"}


def check_board_restock(data):
    # 플랫폼은 색 코드(["b","r"])로 보냄 - 내부(handled_restock, /restock/board/<color>)는
    # 전체 이름이라 여기서 변환. 전체 이름("blue")으로 와도 그대로 받음.
    board_restock = []
    for c in (data.get("board_restock") or []):
        color = BOARD_CODE_TO_COLOR.get(c, c)
        if color in BOARD_COLORS and color not in board_restock:
            board_restock.append(color)

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
                # 재부팅 복원분은 status/retry를 몰라 job_key로 못 걸러냄 -
                # 같은 order_id+color가 이미 큐에 있으면 다시 안 넣음 (중복 픽업 방지)
                if any(j["order_id"] == order_id and j["color"] == color for j in job_queue):
                    log("QUEUE", f"{order_id}({color}, {status}, retry={retry}) 이미 큐에 있음 - 적재 생략")
                else:
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

    # 복원분 중복 적재 방지는 handle_snapshot()에서 job_queue 존재 여부로 처리
    job_queue = fetch_queue_from_server()

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

    # 시작 시 HOME으로 이동 (픽업 전이라 손목 각도는 HOME_WRIST_DEG)
    hx, hy, hz = HOME_XYZ
    loop.run_until_complete(move_and_wait(arm_api, (hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG)),
                                          label="시작 - 초기위치로 이동"))

    log("INIT", f"준비 완료 (픽업:{STATUS_PICK}/{STATUS_MISMATCH}, 컨베이어1:{STATUS_CONVEYOR_1}, "
                f"컨베이어2:{STATUS_CONVEYOR_2}, 큐 {len(job_queue)}건, "
                f"게이트 {'닫힘' if not gate_open else '열림'}) - 대기 시작. Ctrl+C로 종료")

    first_snapshot_handled = False
    try:
        while True:
            try:
                data = snapshot_queue.get(timeout=1)
                handle_snapshot(loop, arm_api, box_api, data)
                first_snapshot_handled = True
            except queue.Empty:
                pass

            # 첫 스냅샷을 받기 전엔 큐를 꺼내지 않음 - 복원된 job이 이미 끝난/사라진 주문인지,
            # 중복인지를 스냅샷으로 먼저 정리(prune/중복체크)한 뒤에 처리하려고
            if first_snapshot_handled and gate_open and job_queue:
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