# -*- coding: utf-8 -*-
"""
=====================================================================
 보드 로봇암 - 최소 픽앤플레이스 (좌표 제어만, 그 외 전부 나중)
=====================================================================
지금 하는 일
  주문 신호 수신 -> 초기위치에서 픽업 위치로 내려감 -> 보드 집음
  -> 트레이 위치로 이동 -> 놓음 -> 초기위치로 복귀
  -> 성공/실패를 warehouse-robotarm-flask의 /report/board-robot-arm 으로 보고
     (Flask가 그걸 cnt_robot_arm/cnt_board_robot_arm CIN으로 Mobius에 올림)

아직 안 하는 것 (전부 나중 단계)
  - 높이 계산 (더미가 줄어드는 것 반영) -> 지금은 PICK_POSE 고정값
  - 색깔 구분 -> 지금은 항상 같은 자리에서 집음
  - 센서로 집기/색 확인
  - cnt_warehouse/cnt_board_dispense 보고 (실제 어떤 색을 넣었는지 - 색 구분 붙을 때 같이)
  - Redis 재고 관리
  - fault 처리 / 재시도

실행 전 확인
  - DobotLab에서 연결 끊어둘 것 (COM 포트 충돌 방지)
  - DobotLink.exe 실행 중이어야 함
  - .env에 MQTT_HOST/PORT/USER/PASS, MQTT_TOPIC, FLASK_BASE_URL
  - 아래 PICK_POSE / PLACE_POSE / HOME_POSE 좌표를 실측값으로 반드시 교체할 것
    (지금 값은 안전 확인 전 임시값 - 그대로 실행하면 위험할 수 있음)
=====================================================================
"""

import asyncio
import json
import os
import queue
import time

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------
# MQTT 설정 - .env에서 로드
# ---------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sjuiot/warehouse/robotarm")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "pc_robot_arm_board")

if not all([MQTT_HOST, MQTT_USER, MQTT_PASS]):
    raise RuntimeError(".env에 MQTT_HOST / MQTT_USER / MQTT_PASS가 비어있음")

# ---------------------------------------------------------------
# 보고용 Flask(warehouse-robotarm-flask) 주소 - .env의 FLASK_BASE_URL 재사용
# ---------------------------------------------------------------
FLASK_BASE_URL = os.getenv("FLASK_BASE_URL")
if not FLASK_BASE_URL:
    raise RuntimeError(".env에 FLASK_BASE_URL이 비어있음 - CIN 보고를 어디로 쏠지 알 수 없음")
REPORT_URL = f"{FLASK_BASE_URL.rstrip('/')}/report/board-robot-arm"
REPORT_TIMEOUT = 5

# ---------------------------------------------------------------
# 어떤 status가 오면 반응할지 - 지금은 assigned로 테스트 중.
# 나중에 실제 명세대로 가면 "tray_reached"/"board_mismatched"로 바꿀 것.
# ---------------------------------------------------------------
TRIGGER_STATUS = "tray_reached"

ARM_PORT = "COM17"
NAMESPACE_ARM = "MagicianLite"

# ⚠️ PICK_POSE/PLACE_POSE는 팀원 dobot_control.py의 set1(COM6) 실측값을 그대로 가져온 것.
#    그 좌표는 COM6에 붙은 "다른" 물리적 로봇팔 기준이라, COM17 보드 로봇암에
#    그대로 적용되는 보장이 없음 - 실물 옆에서 저속/수동 확인 후 실행할 것.
# ⚠️ HOME_POSE는 팀원 코드에 대응 개념이 없어서(팀원은 place 후 LIFT_Z_OFFSET만큼
#    들어올리고 끝냄) 임시값 그대로 둠 - 별도 실측 필요.
HOME_POSE = (200.0, 0.0, 50.0, 0.0)     # 대기(초기) 위치 - 임시값, 실측 필요
PICK_POSE = (135.8, -279.9, 38.0, -64.1)   # 팀원 set1 pickup_slots[1] 실측값
PLACE_POSE = (248.5, -94.7, -19.8, -20.9)  # 팀원 set1 place_slots[1] 실측값

PTP_MODE_JUMP = 0
CALL_TIMEOUT = 5
GRIP_SETTLE_DELAY = 0.5
RELEASE_SETTLE_DELAY = 0.5

snapshot_queue: "queue.Queue" = queue.Queue()
last_seq = -1


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


# ============================== 로봇암 제어 헬퍼 ==============================
async def safe_call(coro, timeout_sec=CALL_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def is_ok(res):
    return not (isinstance(res, str) and (res.startswith("TIMEOUT") or res.startswith("ERROR")))


async def ptp(arm_api, port, pose):
    x, y, z, r = pose
    return await safe_call(arm_api.SetPTPCmd(portName=port, ptpMode=PTP_MODE_JUMP,
                                              x=x, y=y, z=z, r=r, isQueued=False))


async def wait_until_arrived(arm_api, port, target_pose, tolerance=3.0, timeout=8.0):
    tx, ty, tz, tr = target_pose
    start = time.time()
    while time.time() - start < timeout:
        res = await safe_call(arm_api.GetPose(portName=port, isQueued=False))
        if isinstance(res, dict):
            dx = abs(res.get("x", 9999) - tx)
            dy = abs(res.get("y", 9999) - ty)
            dz = abs(res.get("z", 9999) - tz)
            if dx < tolerance and dy < tolerance and dz < tolerance:
                return True
        await asyncio.sleep(0.2)
    return False


async def gripper(arm_api, port, on):
    res = await safe_call(arm_api.SetEndEffectorGripper(portName=port, enable=True,
                                                         on=on, isQueued=False))
    await asyncio.sleep(GRIP_SETTLE_DELAY)
    return res


# ============================== 보드 픽앤플레이스 (최소 버전) ==============================
async def pick_and_place_board(arm_api):
    """
    초기위치 -> 픽업 -> 트레이 위 -> 초기위치.
    성공/실패만 판단, 아직 Mobius 보고 안 함 (다음 단계에서 추가).
    """
    log("ARM", "그리퍼 오픈")
    await gripper(arm_api, ARM_PORT, on=False)

    log("ARM", "픽업 위치로 이동")
    moved = await ptp(arm_api, ARM_PORT, PICK_POSE)
    pick_arrived = await wait_until_arrived(arm_api, ARM_PORT, PICK_POSE)
    log("ARM", f"픽업 위치 도착: {pick_arrived} (명령 결과: {moved})")
    if not pick_arrived:
        log("ARM", "픽업 위치 도착 실패 - 중단")
        await ptp(arm_api, ARM_PORT, HOME_POSE)
        return False

    log("ARM", "보드 집기")
    grabbed = await gripper(arm_api, ARM_PORT, on=True)

    log("ARM", "트레이 위치로 이동")
    moved = await ptp(arm_api, ARM_PORT, PLACE_POSE)
    place_arrived = await wait_until_arrived(arm_api, ARM_PORT, PLACE_POSE)
    log("ARM", f"트레이 위치 도착: {place_arrived} (명령 결과: {moved})")
    if not place_arrived:
        log("ARM", "트레이 위치 도착 실패 - 보드를 든 채로 중단 (수동 확인 필요)")
        return False

    log("ARM", "보드 놓기")
    released = await gripper(arm_api, ARM_PORT, on=False)
    await asyncio.sleep(RELEASE_SETTLE_DELAY)

    log("ARM", "초기위치로 복귀")
    await ptp(arm_api, ARM_PORT, HOME_POSE)
    await wait_until_arrived(arm_api, ARM_PORT, HOME_POSE)

    success = pick_arrived and place_arrived and is_ok(grabbed) and is_ok(released)
    log("ARM", f"결과: {'성공' if success else '실패'}")
    return success


def report_board_robot_arm(order_id, status):
    """
    보드 로봇암 픽앤플레이스 결과를 Flask에 보고 -> Mobius CIN 생성.
    status: "done" | "failed"
    실패해도 예외를 위로 던지지 않음 - 보고 실패가 로봇팔 제어 루프를 죽이면 안 됨.
    """
    try:
        resp = requests.post(
            REPORT_URL,
            json={"order_id": order_id, "status": status},
            timeout=REPORT_TIMEOUT,
        )
        ok = 200 <= resp.status_code < 300
        log("REPORT", f"{'성공' if ok else '실패'} ({resp.status_code}): order_id={order_id}, status={status}")
        if not ok:
            log("REPORT", f"응답 내용: {resp.text}")
        return ok
    except Exception as e:
        log("REPORT", f"요청 자체 실패: {e}")
        return False


async def handle_snapshot(arm_api, data):
    orders = data.get("orders", {})
    for order_id, order in orders.items():
        if order.get("status") != TRIGGER_STATUS or order.get("fault") is not None:
            continue

        log("ARM", f"{order_id}: {TRIGGER_STATUS} 감지 -> 픽앤플레이스 실행")
        success = await pick_and_place_board(arm_api)
        report_board_robot_arm(order_id, "done" if success else "failed")


# ============================== 메인 (동기 - DobotRPC 요구사항) ==============================
def main():
    mqtt_client = build_mqtt_client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    from DobotRPC import RPCClient, DobotlinkAdapter

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
    arm_api = getattr(dobotlink, NAMESPACE_ARM)

    log("INIT", f"로봇암 연결 시도: {ARM_PORT}")
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=ARM_PORT)))
    log("INIT", f"연결 결과: {connect_res}")
    if not is_ok(connect_res):
        log("INIT", "로봇암 연결 실패 - DobotLab 연결 끊겨있는지, DobotLink.exe 켜져있는지 확인")

    log("INIT", f"준비 완료 (트리거: {TRIGGER_STATUS}) - 대기 시작. Ctrl+C로 종료")

    try:
        while True:
            try:
                data = snapshot_queue.get(timeout=1)
            except queue.Empty:
                continue
            loop.run_until_complete(handle_snapshot(arm_api, data))
    except KeyboardInterrupt:
        log("SHUTDOWN", "Ctrl+C로 종료")
    finally:
        log("SHUTDOWN", "정리 중")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=ARM_PORT)))


if __name__ == "__main__":
    main()