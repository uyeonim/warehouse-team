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

전체 흐름
  [보드 픽업 - tray_reached]
    1. cnt_board_status/cnt_{색} = busy
    2. 배출 (A안 높이계산)
    3. cnt_robot_arm/cnt_board_robot_arm = done/failed
    4. 성공 시 cnt_warehouse/cnt_board_dispense
    5. cnt_board_status/cnt_{색} = idle + count-1  -> 플랫폼이 board_packed로 전이
  [트레이 컨베이어 - board_packed]
    6. 컨베이어(Magic Box) section2 구간 구동
    7. cnt_conveyor/cnt_tray_conveyor = {"order_id":..., "section2": "done"|"failed"}

같은 (order_id, status) 조합은 한 번만 처리 (MQTT 재전송 중복 실행 방지).

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

STATUS_PICK = "tray_reached"
STATUS_CONVEYOR = "board_packed"

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
# CONFIG - 트레이 컨베이어 (⚠️ 캘리브레이션 스크립트로 이미 확정된 값이면 여기 반영할 것)
# =================================================================
TRAY_CONVEYOR_INDEX = 0
TRAY_CONVEYOR_SPEED_MM_S = 30
TRAY_CONVEYOR_RUN_TIME = 3.0
TRAY_CONVEYOR_SECTION = "2"

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
handled_status = {}


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
async def run_tray_conveyor_async(box_api):
    try:
        speed_pulses = mm_per_sec_to_pulses(TRAY_CONVEYOR_SPEED_MM_S)
        log("CONV", f"컨베이어 구동 시작 (index={TRAY_CONVEYOR_INDEX}, speed={TRAY_CONVEYOR_SPEED_MM_S}mm/s)")
        start_res = await safe_call(box_api.SetEMotor(
            portName=DOBOT_PORT, index=TRAY_CONVEYOR_INDEX,
            enable=True, speed=speed_pulses, isQueued=False
        ))
        log("CONV", f"시작 명령 결과: {start_res}")

        await asyncio.sleep(TRAY_CONVEYOR_RUN_TIME)

        stop_res = await safe_call(box_api.SetEMotor(
            portName=DOBOT_PORT, index=TRAY_CONVEYOR_INDEX,
            enable=False, speed=0, isQueued=False
        ))
        log("CONV", f"정지 명령 결과: {stop_res}")

        return is_ok(start_res) and is_ok(stop_res)
    except Exception as e:
        log("CONV", f"예외 발생: {e}")
        return False


# ============================== 주문 처리 (동기 래퍼) ==============================
def handle_board_pick(loop, arm_api, order_id, color):
    if color not in BOARD_COLORS or color not in PICK_POSE_BASE:
        log("ARM", f"알 수 없는 색상 '{color}' - 중단")
        return

    count_before = local_stock.get(color, 0)
    if count_before <= 0:
        log("ARM", f"[{color}] 재고 0 - 픽업 불가")
        report_board_status(order_id, color, "empty", count=0)
        report_board_robot_arm(order_id, "failed")
        return

    report_board_status(order_id, color, "busy", count=count_before)
    success = loop.run_until_complete(pick_and_place_board_async(arm_api, color, count_before))
    report_board_robot_arm(order_id, "done" if success else "failed")

    if not success:
        report_board_status(order_id, color, "idle", count=count_before)
        return

    report_board_dispense(order_id, color)
    count_after = count_before - 1
    local_stock[color] = count_after
    report_board_status(order_id, color, "idle", count=count_after)
    log("STOCK", f"[{color}] 재고 {count_before} -> {count_after}")


def handle_tray_conveyor(loop, box_api, order_id):
    log("CONV", f"{order_id}: board_packed 감지 -> 트레이 컨베이어(section{TRAY_CONVEYOR_SECTION}) 구동")
    success = loop.run_until_complete(run_tray_conveyor_async(box_api))
    report_conveyor_tray(order_id, TRAY_CONVEYOR_SECTION, "done" if success else "failed")


def handle_snapshot(loop, arm_api, box_api, data):
    orders = data.get("orders", {})
    for order_id, order in list(orders.items()):
        if order.get("fault") is not None:
            handled_status.pop(order_id, None)
            continue

        status = order.get("status")
        if handled_status.get(order_id) == status:
            continue

        if status == STATUS_PICK:
            color = order.get("board")
            log("ARM", f"{order_id}: {STATUS_PICK} 감지 (색상={color}) -> 처리 시작")
            handle_board_pick(loop, arm_api, order_id, color)
            handled_status[order_id] = status

        elif status == STATUS_CONVEYOR:
            handle_tray_conveyor(loop, box_api, order_id)
            handled_status[order_id] = status

    for order_id in list(handled_status):
        if order_id not in orders:
            handled_status.pop(order_id, None)


# ============================== 메인 ==============================
def main():
    global local_stock
    local_stock = fetch_initial_stock()

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

    log("INIT", f"준비 완료 (픽업:{STATUS_PICK}, 컨베이어:{STATUS_CONVEYOR}) - 대기 시작. Ctrl+C로 종료")

    try:
        while True:
            try:
                data = snapshot_queue.get(timeout=1)
            except queue.Empty:
                continue
            handle_snapshot(loop, arm_api, box_api, data)
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