# -*- coding: utf-8 -*-
"""
=====================================================================
 창고 로봇 제어 — 키캡 팔 2대(세트1·2) + 키캡 벨트 2대 + 트레이 팔 1대
 통신 코드(warehouse_server.py / 게이트웨이)에서 불러 씀. 2026-11-02 기준
=====================================================================

원본: 바탕화면 two_sets_full.py (안정화 로직 유지: isQueued=False, GetPose 로 도착·정지 확인,
그리퍼 대기, DobotRPC 는 한 스레드·한 이벤트루프에서만)

■ 단계별 호출 (주문 status → 함수)
  assigned         → run_tray_arm         빈 트레이를 집어 트레이 벨트 투입 자리에 놓음 (약 12초)
  keycap_dispensed → run_keycap_conveyor  두 세트 벨트 60mm/s · 10초 (약 11초)
  keycap_reached   → run_keycap_arms      키캡 4개를 트레이에 놓으며 하나씩 바로 보고 (2026-11-02 실측 43초)

■ 좌표 — 우선순위: 코드 SETS(10/8 시험 배치) → LAYOUT=design 이면 pose_<이름>_design.json(도면 추정)
  → 실물에서 찍은 pose_<이름>.json (최우선). 지금 키캡 세트는 찍은 값, 트레이 팔은 도면 추정값.
  기준: 반경 230~330mm, 관절1 각도 ±125° 이내 (벗어나면 켤 때 경고)

■ 알람(빨간불) — 움직이기 전, 명령이 무시됐을 때, 도착 실패 뒤, 단계 끝에 자동 해제 (사용자 결정)
  관절 한계를 실제로 넘어선 경우는 해제만으로 안 풀림 → 손으로 옮기거나 read_pose.py <팔> home

■ 두 팔 충돌 방지 — 트레이 자리는 한 번에 한 팔만 (zone lock). 놓으러 가기·빠지기 모두 잠금 안.
  못 빠진 팔이 있으면 다른 팔은 최대 15초 기다리고, 그래도 막혀 있으면 놓지 않음.

■ 키캡 번호 (통신 CNT 기준)
  세트1 COM6: keycap1(EI 선반) = 슬롯1, keycap2(NS 선반) = 슬롯2
  세트2 COM7: keycap3(TF 선반) = 슬롯1, keycap4(PJ 선반) = 슬롯2

■ 실행 환경: C:\Python39\python.exe (DobotRPC 설치됨)
  시연 (점검/전체/단계별):  python demo.py check | run | tray | belts | arms
  단독 테스트:              python dobot_control.py        (벨트 → 키캡 팔)
  한 세트만:                python dobot_control.py set1   (또는 set2)
"""

import asyncio
import json
import os
import time
import math
from DobotRPC import RPCClient, DobotlinkAdapter
from DobotRPC.Utils import loggers as _dobot_loggers
from runtime_config import PROFILE  # .env (기본) 또는 .env.local (WAREHOUSE_ENV_FILE) 을 읽음
from usb_serial import list_ports, resolve_port
import tray_stock

_dobot_loggers.set_use_file(False)  # 실행할 때마다 폴더에 DobotRPC_날짜.log 가 쌓이지 않게 (화면 출력은 그대로)


NAMESPACE_ARM = "MagicianLite"
NAMESPACE_BOX = "MagicBox"

_MM_TO_PULSE = (360.0 / 1.8 * 5.0 * 16.0) / (3.1415926535898 * 32)


def mm_per_sec_to_pulses(speed_mm_s):
    return int(math.floor(speed_mm_s * _MM_TO_PULSE))


PTP_MODE_JUMP = 0   # 올라갔다 옮겨서 내려감 (낮은 곳 → 낮은 곳)
PTP_MODE_MOVJ = 1   # 관절 이동 — move_to 의 마지막 재시도용
PTP_MODE_MOVL = 2  # 직선 이동 (같은 x·y 에서 바로 위로 들기 / 바로 아래로 내리기)
# ※ 2026-09-18 실측: 로봇이 아직 움직이는 중(도착 3mm 전)에 다음 명령을 보내면 무시됨
#   → wait_until_arrived 가 완전히 멈춘 것까지 확인. 트레이 → 벨트처럼 멀리 갈 때는 JUMP.
CONVEYOR_SPEED = 60       # mm/s, 양쪽 공통 (2026-10-08: 가림막으로 끝막이를 세워 50 → 60)
CONVEYOR_RUN_TIME = 10.0  # 초, 양쪽 공통. 가림막이 키캡을 멈춰 세우므로 시간은 넉넉히만 주면 됨
RELEASE_SETTLE_DELAY = 0.5
MOVE_SETTLE_DELAY = 0.5
GRIP_SETTLE_DELAY = 0.5
INTER_KEYCAP_DELAY = 3.0
ZONE_WAIT_SEC = 15.0      # 다른 팔이 트레이 자리에서 못 빠졌을 때 기다려 주는 시간
LIFT_Z_OFFSET = 50.0      # 벨트 위에 떠서 기다리는 높이 (다음에 집을 슬롯 위)
PLACE_LIFT_Z = 30.0       # 트레이에 놓은 뒤 바로 위로 드는 높이 (크게 들면 좌표 범위 알람)
RETREAT_RADIUS = 300.0    # 놓은 자리가 이보다 멀면, 벨트로 가기 전에 몸 쪽(중심에서 이 거리)으로 먼저 당김
# 놓은 뒤 "살짝 들기 → (먼 자리면) 몸 쪽으로 당기기" 를 할지 (.env PLACE_LIFT, 기본 1)
#   1 = 함 (10/1 시험 배치: 놓는 자리 330~367mm 에서 바로 JUMP 하면 명령 무시·알람 → 넣음)
#   0 = 안 함, 놓고 바로 JUMP (JUMP 가 출발할 때 스스로 20mm 들어 올림). 놓은 뒤 "움찔" 없음
#   ※ 0 에서 빠질 때 명령 무시·알람이 반복되면 1 로 되돌릴 것 (참고사항 10장)
PLACE_LIFT = os.environ.get("PLACE_LIFT", "1").strip() != "0"
CALL_TIMEOUT = 5

# 두 팔이 트레이 자리를 번갈아 쓸지 (.env ARMS_TAKE_TURNS, 기본 1)
#   1 = 번갈아 (한 팔이 놓고 빠진 뒤 다른 팔. 트레이 자리가 겹칠 때 — 2026-09-18 시연 기준)
#   0 = 차례 없이 두 팔이 동시에 각자 놓음 (최종 트레이에서 두 팔 놓는 자리가 충분히 떨어져 있으면)
ARMS_TAKE_TURNS = os.environ.get("ARMS_TAKE_TURNS", "1").strip() != "0"

# ---------------------------------------------------------------
# 세트별 설정 — keycaps: (이름, 슬롯 번호, 통신 CNT 의 keycap 번호)
# 아래 좌표는 10/8 시험 배치 값 (기본값). 실제로는 pose_set1.json / pose_set2.json 이 덮어씀
# ---------------------------------------------------------------
_usb_ports = list_ports()


def _port_spec(name, default):
    """(.env <이름>_SERIAL, <이름>_PORT 또는 기본 COM) - 재연결 때 COM 을 다시 찾으려고 설정에 같이 둠"""
    return os.environ.get(f"{name}_SERIAL", "").strip(), (os.environ.get(f"{name}_PORT") or default).strip()


def _port(name, default):
    """.env 의 <이름>_SERIAL(USB 시리얼)이 있으면 그 팔이 지금 꽂힌 COM 을 찾아 씀 (USB 를 다시 꽂아
    COM 번호가 바뀌어도 팔이 뒤바뀌지 않음). 시리얼을 못 찾으면 None — 다른 팔 COM 으로 움직이면 충돌하므로
    예비 COM 으로 넘어가지 않음. 시리얼이 없으면 예전처럼 <이름>_PORT (SET1_PORT / SET2_PORT / TRAY_PORT).
    [11/05] 못 찾아도 끝내지 않음 (예전엔 RuntimeError 로 게이트웨이가 안 켜졌음) - 연결 감시가 5초마다 다시 찾음"""
    serial, fallback = _port_spec(name, default)
    port = resolve_port(serial, fallback, _usb_ports)
    if port is None:
        print(f"[{time.strftime('%H:%M:%S')}] [LINK  ] !! {name} 팔(USB 시리얼 {serial})이 안 보임 - USB/전원 확인 "
              f"(켜진 뒤 자동으로 다시 찾음)")
    return port


SETS = {
    "set1": {
        "port": _port("SET1", "COM6"),
        "conveyor_index": 1,
        # 2026-10-08 가림막(끝막이) 설치 후 다시 찍음
        "pickup_slots": {
            1: (-111.7, -344.4, 32.3, -108.0),
            2: (-106.8, -270.5, 34.1, -111.6),
        },
        "place_slots": {
            1: (-219.9, 245.4, 67.4, 131.8),
            2: (-219.4, 293.7, 69.9, 126.7),
        },
        "keycaps": [("1번 키캡(EI)", 1, 1), ("2번 키캡(NS)", 2, 2)],
    },
    "set2": {
        "port": _port("SET2", "COM7"),
        "conveyor_index": 1,
        "conveyor_reverse": True,  # 세트2 벨트는 반대 방향 (속도·시간은 같음). 2026-11-02 도면 배치에서도 정상
        # 2026-10-08 가림막(끝막이) 설치 후 찍은 시험 배치 좌표
        "pickup_slots": {
            1: (87.0, -327.3, 35.7, -75.2),
            2: (88.3, -253.3, 37.3, -70.8),
        },
        "place_slots": {
            1: (211.8, 247.1, 68.2, 49.3),
            2: (212.5, 297.7, 69.1, 54.4),
        },
        "keycaps": [("3번 키캡(TF)", 1, 3), ("4번 키캡(PJ)", 2, 4)],
    },
}

# 트레이 로봇팔 — 빈 트레이를 쌓아둔 곳에서 집어 벨트에 올림 (status=assigned 때)
# SETS 와 따로 둠 → 키캡 단계에서는 이 팔이 움직이지 않음.
# 좌표는 파일에서 읽음: 지금은 LAYOUT=design 의 도면 추정값. 실물 트레이가 오면 read_pose.py tray 로 찍을 것
# (둘 다 없으면 None → 트레이 팔은 움직이지 않고 실패 보고).
# 여러 더미·층으로 쌓으면 pose_tray.json "stacks" → 남은 개수로 집을 자리 계산 (tray_stock.py)
TRAY = {
    "port": _port("TRAY", "COM8"),
    "pickup_slots": {1: None},   # 빈 트레이 쌓아둔 곳 (stacks 가 있으면 매번 남은 개수로 바꿔 넣음)
    "place_slots": {1: None},    # 벨트 위 놓는 곳
    "keycaps": [("빈 트레이", 1, 1)],  # arm_set 가 쓰는 형식 (이름, 슬롯, 번호)
    # 놓은 뒤 방금 집은 자리 위로 빠지는 높이. 50이면 5층(z 약 105)에서 z 155 → JUMP 한계(zLimit 150) 넘음 (2026-11-08)
    "away_lift_z": 20.0,
}


for _cfg, _name, _default in ((SETS["set1"], "SET1", "COM6"), (SETS["set2"], "SET2", "COM7"), (TRAY, "TRAY", "COM8")):
    _cfg["serial"], _cfg["fallback"] = _port_spec(_name, _default)
ARM_LABEL = {"set1": "키캡 팔1", "set2": "키캡 팔2", "tray": "트레이 팔"}


def load_poses(cfg, name):
    """pose_<name>.json 이 있으면 그 좌표를 씀 (read_pose.py 로 찍으면 생기는 파일).
    아직 자리가 확정되지 않은 트레이 팔처럼, 코드를 고치지 않고 좌표만 바꾸려고."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"pose_{name}.json")
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return cfg
    for group in ("pickup_slots", "place_slots"):
        for slot, pose in (saved.get(group) or {}).items():
            if pose:
                cfg[group][int(slot)] = tuple(pose)
    return cfg


# 좌표 우선순위: 코드 SETS → (LAYOUT=design 이면) 도면 예상 좌표 pose_<이름>_design.json → 실물에서 찍은 pose_<이름>.json
LAYOUT = os.environ.get("LAYOUT", "test").strip().lower()
if LAYOUT == "design":
    for _name, _cfg in {**SETS, "tray": TRAY}.items():
        load_poses(_cfg, f"{_name}_design")
for _name, _cfg in {**SETS, "tray": TRAY}.items():  # 실물에서 찍은 좌표가 있으면 그게 최우선
    load_poses(_cfg, _name)
if tray_stock.enabled() and not TRAY["pickup_slots"][1]:  # 더미만 찍었어도 트레이 팔을 쓰는 것으로 봄
    TRAY["pickup_slots"][1] = tray_stock.STACKS[0][0]
TRAY["travel_z"] = tray_stock.travel_z()


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag:<6}] {msg}")


# ---------------------------------------------------------------
# 알람(빨간불) — GetAlarmsState 의 state 는 16바이트 비트맵. 바이트×8 + 비트 = 알람 번호
# 2026-11-02 실측: 0x12·0x22 = 목표/이동이 관절 한계 밖, 트레이 팔 실패 때 0x51·0x52 = 관절2·3 탈조(충돌)
# ---------------------------------------------------------------
def alarm_codes(state):
    values = state.get("state") if isinstance(state, dict) else None
    if not isinstance(values, list):
        return []
    return [i * 8 + b for i, v in enumerate(values) if isinstance(v, int) for b in range(8) if v >> b & 1]


# Dobot 공식 알람 설명서 (Dobot Magician ALARM 说明 V1.0.0, 2018-12-07) 기준 이름
ALARM_NAMES = {
    0x00: "재시작 후 남은 알람", 0x01: "알 수 없는 명령 받음", 0x02: "파일 시스템 오류(재시작 필요)",
    0x03: "내부 통신 실패(재시작 필요)", 0x04: "각도 센서 읽기 오류(전원 껐다 켜기)",
    0x10: "목표점이 특이 자세(계획)", 0x11: "목표점이 작업 범위 밖(계획)", 0x12: "목표점이 관절 한계 밖(계획)",
    0x13: "같은 점 반복(계획)", 0x14: "원호 입력 오류(계획)", 0x15: "JUMP 높이 설정 오류",
    0x20: "이동 중 특이 자세", 0x21: "이동 중 작업 범위 벗어남", 0x22: "이동 중 관절 한계 넘음",
    0x30: "관절1 속도 초과", 0x31: "관절2 속도 초과", 0x32: "관절3 속도 초과", 0x33: "관절4 속도 초과",
    0x40: "관절1 +한계", 0x41: "관절1 -한계", 0x42: "관절2 +한계", 0x43: "관절2 -한계",
    0x44: "관절3 +한계", 0x45: "관절3 -한계", 0x46: "관절4 +한계", 0x47: "관절4 -한계",
    0x48: "팔 링크 +한계", 0x49: "팔 링크 -한계",
    0x50: "관절1 탈조(충돌)", 0x51: "관절2 탈조(충돌)", 0x52: "관절3 탈조(충돌)", 0x53: "관절4 탈조(충돌)",
}
LOSE_STEP_ALARMS = range(0x50, 0x54)  # 부딪혀서 모터가 위치를 놓침 → 해제만으로는 위치가 틀어진 채 남음, 홈 복귀 필요


def describe_alarm(code):
    name = ALARM_NAMES.get(code, "기타")
    return f"0x{code:02X} {name}" + (" → 홈 복귀 필요" if code in LOSE_STEP_ALARMS else "")


async def clear_alarms_now(arm_api, port, why):
    """알람이 있으면 무슨 알람인지 남기고 바로 해제 (잔류 알람 자동 해제 — 사용자 결정).
    알람이 남아 있으면 그 팔의 다음 명령이 전부 무시되므로, 동작 중에도 해제해서 이어가게 함."""
    state = await safe_call(arm_api.GetAlarmsState(portName=port), 3)
    codes = alarm_codes(state)
    if codes:
        await safe_call(arm_api.ClearAllAlarmsState(portName=port), 3)
        log(port, f"  알람 자동 해제 ({why}): {', '.join(describe_alarm(c) for c in codes)}")
    return codes


# 좌표 점검 기준 (2026-11-02 실측): 330mm 넘으면 명령 무시가 잦고, 230mm 안쪽은 위로 들 때 0x12 알람,
# 관절1(팔 방향 각도)은 135° 에서 한계 알람 → 125° 넘으면 경고
REACH_MAX, REACH_MIN, ANGLE_MAX = 330.0, 230.0, 125.0


def check_poses(cfgs):
    """켤 때 좌표가 위 기준을 벗어나면 경고만 출력 (동작은 막지 않음)."""
    for name, cfg in cfgs.items():
        for group in ("pickup_slots", "place_slots"):
            for slot, pose in cfg[group].items():
                if not pose:
                    continue
                x, y = pose[0], pose[1]
                reach, angle = math.hypot(x, y), math.degrees(math.atan2(y, x))
                where = f"{name} {group}[{slot}] 반경 {reach:.0f}mm 각도 {angle:.0f}°"
                if reach > REACH_MAX:
                    log("WARN", f"{where} → {REACH_MAX:.0f}mm 초과: 명령 무시·알람이 날 수 있음")
                elif reach < REACH_MIN:
                    log("WARN", f"{where} → {REACH_MIN:.0f}mm 미만: 위로 들 때 좌표 범위 알람이 날 수 있음")
                if abs(angle) > ANGLE_MAX:
                    log("WARN", f"{where} → 관절1 한계(135°) 근처")


def is_ok(res):
    """safe_call 결과가 시간초과/오류가 아니면 True."""
    return not (isinstance(res, str) and (res.startswith("TIMEOUT") or res.startswith("ERROR")))


async def safe_call(coro, timeout_sec=CALL_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


async def ptp(arm_api, port, pose, is_queued=False, mode=PTP_MODE_JUMP):
    x, y, z, r = pose
    return await safe_call(arm_api.SetPTPCmd(portName=port, ptpMode=mode,
                                              x=x, y=y, z=z, r=r, isQueued=is_queued))


JUMP_HEIGHT = 20.0   # Dobot 기본 JUMP 들어올림 높이 (2026-11-08 R4 실측: jumpHeight 20, zLimit 150)
JUMP_Z_LIMIT = 150.0


async def set_jump_height(arm_api, port, height):
    """JUMP 때 들어올리는 높이. 정점은 zLimit(150)을 넘지 않음."""
    return await safe_call(arm_api.SetPTPJumpParams(portName=port, jumpHeight=height,
                                                    zLimit=JUMP_Z_LIMIT, isQueued=False))


TRAVEL_MIN_LIFT = 10.0  # 목표가 이미 travel_z 근처일 때도 이만큼은 들고 감


async def use_travel_height(arm_api, port, cfg, target, min_lift=TRAVEL_MIN_LIFT):
    """travel_z 가 있는 팔(트레이 팔)은 target 으로 가는 JUMP 의 정점을 travel_z 로.
    JUMP 정점 = 출발·도착 중 높은 쪽 + jumpHeight (zLimit 150 까지) 로 보고 높이를 맞춤.
    - 기본 20mm 만 들면 벨트(낮은 곳)에서 돌아갈 때 더미 옆면을 침 (2026-11-08)
    - 출발 높이만 보고 크게 들면 5층 위로 빠질 때 정점이 150 에 걸려 0x12 → MOVJ 재시도가 놓은 트레이를 침 (2026-11-08)"""
    travel_z = cfg.get("travel_z")
    if not travel_z:
        return
    pose = await safe_call(arm_api.GetPose(portName=port, isQueued=False))
    z = pose["z"] if isinstance(pose, dict) else travel_z
    await set_jump_height(arm_api, port, max(min_lift, travel_z - max(z, target[2])))


async def wait_until_arrived(arm_api, port, target_pose, tolerance=3.0, timeout=8.0):
    """목표 근처에 오고 + 완전히 멈춘 것까지 확인 (움직이는 중에 다음 명령을 보내면 로봇이 무시함, 2026-09-18 실측)."""
    tx, ty, tz, tr = target_pose
    start = time.time()
    first_pose = last_pose = None
    while time.time() - start < timeout:
        res = await safe_call(arm_api.GetPose(portName=port, isQueued=False))
        if isinstance(res, dict):
            near = all(abs(res.get(k, 9999) - t) < tolerance for k, t in (("x", tx), ("y", ty), ("z", tz)))
            still = last_pose is not None and _same_pose(res, last_pose)
            first_pose = first_pose or res
            last_pose = res
            if near and still:
                await asyncio.sleep(MOVE_SETTLE_DELAY)
                return True, res
            if not near and time.time() - start > 1.5 and _same_pose(res, first_pose):
                return False, res  # 1.5초 동안 꿈쩍도 안 함 → 명령이 무시된 것. 8초 기다리지 않음
        await asyncio.sleep(0.2)
    return False, last_pose


def _same_pose(a, b, eps=0.2):
    return all(abs(a.get(k, 0) - b.get(k, 9999)) < eps for k in ("x", "y", "z"))


async def move_to(arm_api, port, pose, mode=PTP_MODE_JUMP, fallback=True):
    """이동 + 도착 확인. 로봇이 명령을 무시하고 꿈쩍도 안 했으면 한 번 더, 그래도 안 되면 MOVJ 로 재시도.
    fallback=False 면 MOVJ 로 바꾸지 않고 같은 방식으로만 재시도
      (키캡을 들고 가는 구간: MOVJ 는 경로가 달라 엉뚱한 곳에서 놓을 수 있음 — 2026-10-01 실측)
    반환: (명령 결과, 도착 여부, 현재 좌표)"""
    before = await safe_call(arm_api.GetPose(portName=port, isQueued=False))
    res = await ptp(arm_api, port, pose, mode=mode)
    arrived, now = await wait_until_arrived(arm_api, port, pose)
    for retry_mode in ((mode, PTP_MODE_MOVJ) if fallback else (mode, mode)):
        if arrived:
            break
        if not (isinstance(before, dict) and isinstance(now, dict) and _same_pose(before, now, eps=2.0)):
            break  # 움직이긴 했는데 못 감(알람·장애물) → 재시도하면 위험하니 그대로 실패
        log(port, f"  (명령이 무시됨 → 다시 보냄, 방식 {'MOVJ' if retry_mode == PTP_MODE_MOVJ else '같은 방식'})")
        await clear_alarms_now(arm_api, port, "명령 무시")  # 알람이 남아 있으면 다시 보내도 또 무시됨
        await asyncio.sleep(MOVE_SETTLE_DELAY)
        res = await ptp(arm_api, port, pose, mode=retry_mode)
        arrived, now = await wait_until_arrived(arm_api, port, pose)
    if not arrived:
        # 끝내 못 갔어도 빨간불은 꺼 둠 → 이어지는 동작(되돌리기·다음 키캡)이 무시되지 않게 (2026-11-02 시연)
        await clear_alarms_now(arm_api, port, "도착 실패")
    return res, arrived, now


async def gripper(arm_api, port, on, is_queued=False):
    res = await safe_call(arm_api.SetEndEffectorGripper(portName=port, enable=True,
                                                         on=on, isQueued=is_queued))
    await asyncio.sleep(GRIP_SETTLE_DELAY)
    return res


async def conveyor(box_api, port, index, enable, speed_mm_s, is_queued=False):
    speed_pulses = mm_per_sec_to_pulses(speed_mm_s) if enable else 0
    return await safe_call(box_api.SetEMotor(portName=port, index=index, enable=enable,
                                              speed=speed_pulses, isQueued=is_queued))


# =====================================================================
# 세트 1개 실행 로직 — set1, set2 공용 (동작은 원본과 동일, 결과만 모아서 돌려줌)
# =====================================================================

class _NoZone:
    """한 세트만 돌릴 때: 트레이 자리 차례 기다리기 없음."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def new_place_zone():
    """두 세트가 같이 쓰는 트레이 자리. 한 번에 한 팔만 들어감 (기다린 순서대로)."""
    return {"lock": asyncio.Lock(), "blocked_by": None}


async def belt_set(arm_api, box_api, cfg):
    """세트 1개의 벨트만 1회 구동. 반환: {keycap번호: bool}.
    벨트에 도착 센서가 없으므로 "모터 명령이 정상으로 끝났나"로만 판단."""
    port = cfg["port"]
    numbers = [no for _, _, no in cfg["keycaps"]]

    log(port, "그리퍼 오픈 상태로 시작")
    log(port, f"  결과: {await gripper(arm_api, port, on=False)}")

    reverse = cfg.get("conveyor_reverse", False)
    log(port, f"컨베이어 구동 (1회{', 반대 방향' if reverse else ''})")
    started = stopped = "ERROR: not executed"
    try:
        started = await conveyor(box_api, port, cfg['conveyor_index'], True,
                                 -CONVEYOR_SPEED if reverse else CONVEYOR_SPEED)
        log(port, f"  시작: {started}")
        await asyncio.sleep(CONVEYOR_RUN_TIME)
    finally:
        # 중간에 에러·취소가 나도 벨트는 꼭 세움 (시작 명령이 시간초과여도 모터는 켜졌을 수 있음)
        stopped = await conveyor(box_api, port, cfg['conveyor_index'], False, 0)
        log(port, f"  정지: {stopped}")

    conveyor_ok = is_ok(started) and is_ok(stopped)
    return {no: conveyor_ok for no in numbers}


async def arm_set(arm_api, cfg, zone=None, on_result=None):
    """세트 1개의 로봇팔: 키캡 2개를 벨트에서 집어 놓는 곳에 놓음. 반환: {keycap번호: bool}
    zone: 두 세트를 같이 돌릴 때 공유하는 트레이 자리 (new_place_zone()). 없으면 차례 없이 혼자 동작.
    on_result(keycap번호, 성공여부): 키캡 하나 끝날 때마다 바로 호출 (그때그때 보고용)."""
    port = cfg["port"]
    results = {}
    lift = cfg.get("final_lift_z", PLACE_LIFT_Z)
    # 집으러 내려가기 전에 반드시 열어 둠 (닫힌 채로 내려가면 키캡을 못 잡음)
    log(port, f"그리퍼 열기: {await gripper(arm_api, port, on=False)}")
    keycaps = cfg["keycaps"]
    left = True  # 직전 키캡 뒤 트레이 자리에서 빠졌는지
    for i, (letter, slot_no, keycap_no) in enumerate(keycaps):
        log(port, f"키캡 '{letter}' (슬롯 {slot_no}) → place")

        # 1) 집기 — 각자 자기 벨트라서 두 팔이 동시에 해도 됨
        # 두 번째부터는 이미 그 슬롯 바로 위에 떠 있으므로 곧장 아래로 내림
        # 단, 앞에서 트레이 자리를 못 빠졌으면 엉뚱한 곳에 있으므로 직선(MOVL) 말고 JUMP 로 (2026-11-02 시연: 직선 이동이 관절 한계에 걸림)
        pick_mode = PTP_MODE_MOVL if i > 0 and left else PTP_MODE_JUMP
        if pick_mode == PTP_MODE_JUMP:
            await use_travel_height(arm_api, port, cfg, cfg['pickup_slots'][slot_no])
        # 트레이 팔은 MOVJ 재시도 안 함 — 들지 않고 관절째 돌아서 더미·놓은 트레이를 침 (2026-11-08)
        moved, pick_arrived, pose = await move_to(arm_api, port, cfg['pickup_slots'][slot_no], mode=pick_mode,
                                                  fallback=not cfg.get("travel_z"))
        log(port, f"  슬롯 이동 명령: {moved}")
        log(port, f"  슬롯 실제 도착: {pick_arrived} (현재 좌표: {pose})")
        if pick_arrived and zone and zone["blocked_by"] == port:
            zone["blocked_by"] = None  # 벨트까지 왔으니 트레이 자리에서 빠진 것 → 막음 해제
            log(port, "  트레이 자리에서 빠진 것 확인 → 다른 팔 다시 들어가도 됨")
        # 집는 자리에 못 갔으면 엉뚱한 곳에서 그리퍼를 닫지 않음
        grabbed = await gripper(arm_api, port, on=True) if pick_arrived else "ERROR: pickup not reached"
        log(port, f"  집기: {grabbed}")

        # 2) 놓기 — 트레이 자리는 두 팔이 같이 쓰므로 한 번에 한 팔만 (먼저 집은 팔이 먼저)
        place_arrived, released = False, "SKIP"
        # 다른 팔이 트레이 자리에 멈춰 있으면 바로 포기하지 말고 잠깐 기다림 — 그 팔이 다음 집기 자리로 가면 막음이 풀림
        # (2026-11-02 시연: 세트2가 2초 뒤 빠졌는데 세트1이 먼저 포기해서 4개 다 실패)
        if zone and pick_arrived and zone["blocked_by"] not in (None, port):
            log(port, f"  트레이 자리에 {zone['blocked_by']} 팔이 있음 → 최대 {ZONE_WAIT_SEC:.0f}초 기다림")
            waited = 0.0
            while zone["blocked_by"] not in (None, port) and waited < ZONE_WAIT_SEC:
                await asyncio.sleep(0.5)
                waited += 0.5
        async with (zone["lock"] if zone else _NoZone()):
            if zone and zone["blocked_by"]:
                log(port, f"  !! 트레이 자리에 {zone['blocked_by']} 팔이 멈춰 있음 → 충돌 방지로 놓지 않음 (사람 조치 필요)")
            elif not pick_arrived or not is_ok(grabbed):
                log(port, "  !! 집는 자리에 못 갔음 → 트레이로 가지 않음 (다른 팔 차례를 막지 않게)")
            else:
                # 키캡을 들고 가는 구간은 JUMP 만 (MOVJ 로 바꾸면 경로가 달라 엉뚱한 곳에서 놓게 됨)
                # 트레이 팔: 옆 더미 위를 넘어가게. 5층에서 20mm 만 들면 아래 트레이에 살짝 닿음 → 최소 30mm (2026-11-08)
                await use_travel_height(arm_api, port, cfg, cfg['place_slots'][slot_no], min_lift=30.0)
                moved, place_arrived, pose = await move_to(arm_api, port, cfg['place_slots'][slot_no],
                                                           fallback=False)
                log(port, f"  place 이동 명령: {moved}")
                log(port, f"  place 실제 도착: {place_arrived} (현재 좌표: {pose})")
                if not place_arrived:
                    # 못 갔는데 그 자리에서 그리퍼를 열면 키캡이 어딘지 모를 곳에 떨어짐
                    # → 집었던 벨트 자리로 되돌려 놓음 (사람이 찾기 쉽게)
                    log(port, "  !! 놓는 자리에 못 감 → 집었던 벨트 자리로 되돌려 놓음")
                    await use_travel_height(arm_api, port, cfg, cfg['pickup_slots'][slot_no])
                    _, back, pose = await move_to(arm_api, port, cfg['pickup_slots'][slot_no], fallback=False)
                    log(port, f"  되돌리기: {back}" + ("" if back else f" (현재 좌표: {pose} — 이 근처에 떨어짐)"))
                    await gripper(arm_api, port, on=False)
                    released = "SKIP"  # 놓는 자리에 못 놨으므로 실패
                else:
                    released = await gripper(arm_api, port, on=False)
                    log(port, f"  놓기: {released}")
                    await asyncio.sleep(RELEASE_SETTLE_DELAY)

                # 3) 트레이 자리 비우기: 살짝 위로 → 다음에 집을 벨트 위로 점프해서 빠짐.
                #    트레이 자리가 팔을 거의 끝까지 뻗는 곳이라, 거기서 높이 들면 구동 범위 알람이 남 → 조금만 듦
                #    빠진 게 확인돼야 다른 팔 차례
                if place_arrived and PLACE_LIFT:  # 못 갔으면 그 자리에 있지도 않으니 들어올릴 것도 없음
                    px, py, pz, pr = cfg['place_slots'][slot_no]
                    up = (px, py, pz + lift, pr)
                    _, lifted, pose = await move_to(arm_api, port, up, mode=PTP_MODE_MOVL)
                    log(port, f"  위로 들기: {lifted}" + ("" if lifted else f" (현재 좌표: {pose})"))
                    # 팔을 많이 뻗은 자리(중심에서 멀리)면 몸 쪽으로 먼저 당김.
                    # 그 상태로 바로 벨트로 점프하면 명령이 무시되고 구동범위 알람이 쌓임 (2026-10-01 실측)
                    reach = math.hypot(px, py)
                    if reach > RETREAT_RADIUS:
                        k = RETREAT_RADIUS / reach
                        pulled = (px * k, py * k, pz + lift, pr)
                        _, ok_pull, pose = await move_to(arm_api, port, pulled, mode=PTP_MODE_MOVL)
                        log(port, f"  몸 쪽으로 당기기: {ok_pull}" + ("" if ok_pull else f" (현재 좌표: {pose})"))
                next_slot = keycaps[i + 1][1] if i + 1 < len(keycaps) else keycaps[0][1]
                nx, ny, nz, nr = cfg['pickup_slots'][next_slot]
                away = (nx, ny, nz + cfg.get("away_lift_z", LIFT_Z_OFFSET), nr)
                await use_travel_height(arm_api, port, cfg, away)
                _, left, pose = await move_to(arm_api, port, away, fallback=not cfg.get("travel_z"))  # JUMP
                log(port, f"  트레이 자리에서 빠짐: {left}" + ("" if left else f" (현재 좌표: {pose})"))
                if not left and zone:
                    zone["blocked_by"] = port  # 못 빠졌으면 다른 팔이 들어오지 않게 막음

        # 팔이 픽업·플레이스 위치에 실제로 도착했고 그리퍼 명령이 정상이면 성공
        # 트레이 자리에서 못 빠졌으면 실패 — 팔이 트레이 위에 남은 채 done 을 보내면 다음 단계(트레이 벨트)가 출발해 부딪힘
        ok = bool(pick_arrived and place_arrived and is_ok(grabbed) and released != "SKIP" and is_ok(released) and left)
        results[keycap_no] = ok
        log(port, f"  keycap{keycap_no} 결과: {'성공' if ok else '실패'}")
        if on_result:
            on_result(keycap_no, ok)  # 끝나는 대로 바로 보고 (4개 다 끝날 때까지 기다리지 않음)

        if not zone and i < len(keycaps) - 1:
            await asyncio.sleep(INTER_KEYCAP_DELAY)  # 한 세트만 돌릴 때는 원래처럼 쉬었다가

    await gripper(arm_api, port, on=False)
    await safe_call(arm_api.SetEndEffectorGripper(portName=port, enable=False, on=False, isQueued=False))
    if cfg.get("travel_z"):
        await set_jump_height(arm_api, port, JUMP_HEIGHT)  # 다른 프로그램(DobotLab 등)이 쓰는 기본값으로 되돌림
    await clear_alarms_now(arm_api, port, "단계 끝 정리")  # 그리퍼 여닫을 때 생기는 잔류 알람까지 정리

    log(port, "완료")
    return results


async def run_set(arm_api, box_api, cfg):
    """단독 테스트용: 세트 1개 벨트 → 로봇팔 이어서."""
    belt = await belt_set(arm_api, box_api, cfg)
    pack = await arm_set(arm_api, cfg) if all(belt.values()) else {no: False for no in belt}
    return {"conveyor": belt, "pack": pack}


async def all_belts(arm_api, box_api):
    """keycap_dispensed: 두 세트 벨트 동시 구동. {1~4: bool}"""
    merged = {}
    for res in await asyncio.gather(*[belt_set(arm_api, box_api, cfg) for cfg in SETS.values()]):
        merged.update(res)
    return merged


async def all_arms(arm_api, on_result=None):
    """keycap_reached: 두 세트 로봇팔 동시. 트레이 자리는 번갈아 — 집기 동작을 먼저 끝낸 팔이 먼저
    (그리퍼 센서 없음: 좌표 도착 + 그리퍼 명령 응답 기준). 매번 1,3,2,4 또는 3,1,4,2. {1~4: bool}"""
    zone = new_place_zone() if ARMS_TAKE_TURNS else None
    merged = {}
    for res in await asyncio.gather(*[arm_set(arm_api, cfg, zone, on_result) for cfg in SETS.values()]):
        merged.update(res)
    return merged


async def run_all_sets(arm_api, box_api):
    """단독 테스트용: 두 세트 벨트 → 로봇팔."""
    belt = await all_belts(arm_api, box_api)
    pack = await all_arms(arm_api) if all(belt.values()) else {no: False for no in belt}  # 벨트 실패면 팔은 안 움직임
    return {"conveyor": belt, "pack": pack}


# =====================================================================
# 통신 코드가 부르는 함수 — 모두 같은 스레드·같은 이벤트루프에서 실행 (DobotRPC 규칙)
# =====================================================================

_loop = None
_arm_api = None
_box_api = None
_connected = {}  # 팔 이름 → 설정. 끝날 때 연결한 팔 전부 (트레이 팔 포함) 해제하려고 기억


def arms():
    """지금 쓰는 팔들 {이름: 설정}. 트레이 팔은 좌표가 찍혀 있을 때만 (pose_tray.json)."""
    out = dict(SETS)
    if TRAY["pickup_slots"][1] and TRAY["place_slots"][1]:
        out["tray"] = TRAY
    return out


def _init_api():
    """이벤트루프와 DobotLink 연결 객체는 한 번만 만듦 (보드 main 과 같음). 예전엔 재연결 때마다 새로 만들었음"""
    global _loop, _arm_api, _box_api
    if _loop is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    if _arm_api is None:
        dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
        _arm_api = getattr(dobotlink, NAMESPACE_ARM)
        _box_api = getattr(dobotlink, NAMESPACE_BOX)


def connect(cfgs=None):
    """프로그램 시작 때 한 번. 쓰는 팔 전부 연결 (cfgs 를 주면 그것만). 연결 못 한 팔 이름 목록 반환 -
    그 팔은 link_watchdog 가 5초마다 다시 시도함 (끝내지 않음)."""
    _init_api()
    targets = cfgs if cfgs is not None else arms()
    log("INIT", f"로봇팔 연결: {', '.join(targets)}  (좌표: {LAYOUT})")
    check_poses(targets)
    return [key for key, cfg in targets.items() if not _connect_one(key, cfg)]


# ---------------------------------------------------------------
# [11/05] 연결 감시 / 자동 재연결 - 보드 main(link_watchdog·reconnect_robot)과 같은 방식
#   USB 를 뺐다 꽂거나 로봇 전원을 껐다 켜면 DobotLink 연결이 끊김. 예전엔 일을 시킬 때에야 알았고,
#   COM 번호가 바뀌면 게이트웨이를 다시 켜야 했음. 이제 대기 중에도 확인하고, 끊긴 팔만 시리얼로 COM 을
#   다시 찾아 끊고 → 다시 연결. 끊긴 동안 heartbeat 는 link_lost (상태판·점검에 "연결 끊김")
# ---------------------------------------------------------------
LINK_CHECK_SEC = 10      # 정상일 때 확인 간격
LINK_RETRY_SEC = 5       # 끊겼을 때 재연결 시도 간격
LINK_TIMEOUT_LIMIT = 2   # 대기 중 GetPose TIMEOUT 이 이만큼 연달아 나면 끊김으로 봄
_lost = {}               # 팔 이름 -> 끊김을 처음 안 시각
_timeouts = {}
_last_link_check = 0.0


def _mark_lost(key, why):
    if key not in _lost:
        _lost[key] = time.time()
        log("LINK", f"!! {ARM_LABEL.get(key, key)} 연결 끊김 ({why}) - {LINK_RETRY_SEC}초마다 재연결 시도")


def _connect_one(key, cfg):
    """팔 하나: 시리얼로 COM 다시 찾기 → 끊기 → 연결. 성공하면 True"""
    port = resolve_port(cfg.get("serial"), cfg.get("fallback"))
    if port is None:
        _mark_lost(key, f"USB 시리얼 {cfg.get('serial')} 안 보임 - USB/전원 확인")
        return False
    if port != cfg.get("port"):
        if cfg.get("port"):
            log("LINK", f"{ARM_LABEL.get(key, key)} COM 번호: {cfg['port']} -> {port}")
        cfg["port"] = port
    # 끊는 걸 먼저: DobotLink 가 끊긴 포트를 아직 열린 걸로 보고 연결을 거부할 수 있음 (처음 연결 땐 오류만 나고 무해)
    _loop.run_until_complete(safe_call(_arm_api.DisconnectDobot(portName=port)))
    res = _loop.run_until_complete(safe_call(_arm_api.ConnectDobot(portName=port)))
    _connected[key] = cfg
    if not is_ok(res):
        log("INIT", f"{port} ({key}) 연결 실패: {res}")
        _mark_lost(key, res)
        return False
    log("INIT", f"{port} ({key}): {res}")
    if cfg.get("travel_z"):  # 지난번에 도중에 꺼져 JUMP 높이가 크게 남아 있었어도 기본값으로 (USB 재연결 때도)
        _loop.run_until_complete(set_jump_height(_arm_api, port, JUMP_HEIGHT))
    if _lost.pop(key, None) is not None:
        log("LINK", f"{ARM_LABEL.get(key, key)} 재연결 성공")
    _timeouts[key] = 0
    return True


def link_watchdog():
    """게이트웨이가 주문을 기다리는 동안(작업 사이) 매 폴링마다 부름. 정상이면 LINK_CHECK_SEC 마다 GetPose 로
    확인, 끊긴 팔이 있으면 LINK_RETRY_SEC 마다 그 팔만 재연결. 오류(141 등)는 바로, TIMEOUT 은 연달아 났을 때 끊김"""
    global _last_link_check
    if _loop is None:
        return
    now = time.time()
    if now - _last_link_check < (LINK_RETRY_SEC if _lost else LINK_CHECK_SEC):
        return
    _last_link_check = now
    for key, cfg in arms().items():
        if key in _lost or not cfg.get("port"):
            if _connect_one(key, cfg):
                _loop.run_until_complete(clear_alarms_now(_arm_api, cfg["port"], "재연결 뒤"))
            continue
        res = _loop.run_until_complete(safe_call(_arm_api.GetPose(portName=cfg["port"], isQueued=False), 3))
        if isinstance(res, dict):
            _timeouts[key] = 0
        elif res == "TIMEOUT":
            _timeouts[key] = _timeouts.get(key, 0) + 1
            if _timeouts[key] >= LINK_TIMEOUT_LIMIT:
                _mark_lost(key, f"GetPose TIMEOUT {_timeouts[key]}회 연속")
        else:
            _mark_lost(key, res)


def link_problem():
    """끊긴 팔 설명 (없으면 None) - 게이트웨이가 heartbeat link_lost 작업 문구로 씀"""
    if not _lost:
        return None
    return ", ".join(f"{ARM_LABEL.get(k, k)} 연결 끊김" for k in _lost)


def keepalive():
    """게이트웨이가 주문을 기다리는 동안 매 폴링마다 호출.
    DobotLink 연결(websocket)은 이벤트루프가 돌 때만 20초 간격 살아있음 신호를 주고받음.
    오래 안 돌리면 연결이 끊겨 다음 명령이 전부 시간초과 → 2026-09-18 19:33 벨트 4개 failed 사례."""
    if _loop is not None:
        _loop.run_until_complete(asyncio.sleep(0.05))


def ensure_connected(cfgs=None):
    """로봇을 움직이기 전에 쓸 팔들이 응답하는지 확인. 하나라도 응답 없으면 새로 연결.
    cfgs 를 주면 그 팔만 확인 (예: 트레이 팔만 쓸 때 키캡 세트는 안 봄)."""
    targets = cfgs if cfgs is not None else arms()
    if _loop is None:
        connect(targets)
        return
    for key, cfg in targets.items():   # [11/05] 응답 없는 팔만 다시 연결 (예전엔 전부 새로 연결)
        res = None
        if key not in _lost and cfg.get("port"):
            res = _loop.run_until_complete(safe_call(_arm_api.GetPose(portName=cfg["port"], isQueued=False), 3))
        if not isinstance(res, dict):
            log("WARN", f"{cfg.get('port')} ({key}) 응답 없음({res}) → 다시 연결")
            _connect_one(key, cfg)
    clear_alarms({k: c for k, c in targets.items() if c.get("port")})


def clear_alarms(cfgs=None):
    """남아 있는 알람(빨간불) 해제. 쌓여 있으면 다음 이동 명령이 통째로 무시됨.
    ※ 관절 범위를 실제로 벗어나 있으면 이것만으로는 안 풀림 → 사람이 옮기거나 홈 복귀 필요."""
    for cfg in (cfgs if cfgs is not None else arms()).values():
        _loop.run_until_complete(clear_alarms_now(_arm_api, cfg["port"], "움직이기 전"))


def disconnect():
    """프로그램 끝날 때 한 번."""
    if _loop is None:
        return
    log("INIT", "연결 해제")
    for cfg in _connected.values():
        if not cfg.get("port"):
            continue
        if "conveyor_index" in cfg:  # Ctrl+C 로 벨트 도중 끊겨도 벨트는 세움
            _loop.run_until_complete(conveyor(_box_api, cfg["port"], cfg["conveyor_index"], False, 0))
        if cfg.get("travel_z"):  # Ctrl+C 로 도중에 끊겨도 JUMP 높이는 기본값으로 (DobotLab 등에서 쓸 때 안 튀게)
            _loop.run_until_complete(set_jump_height(_arm_api, cfg["port"], JUMP_HEIGHT))
        _loop.run_until_complete(safe_call(_arm_api.DisconnectDobot(portName=cfg["port"])))
    _connected.clear()


def run_keycap_conveyor(order_id):
    """keycap_dispensed 때 호출. 두 세트 벨트만 돌리고 결과 {1~4: bool} 반환 (약 11초)."""
    ensure_connected(SETS)
    return _loop.run_until_complete(all_belts(_arm_api, _box_api))


def run_tray_arm(order_id):
    """assigned 때 호출. 트레이 팔이 빈 트레이를 집어 벨트에 올림. 성공 여부 반환.
    좌표는 pose_tray.json (찍은 값) 또는 pose_tray_design.json (LAYOUT=design). 없으면 움직이지 않고 실패.
    더미(stacks)가 있으면 남은 개수로 집을 더미·층을 정하고, 성공하면 하나 뺌.
    [11/09] 0개면 움직이지 않고 tray_stock.WaitForRefill - 실패 보고(fault) 안 함. 상태판에서 채우면 다음 시도에 집음
    (예전: 0개 -> tray_load_failed -> fault 해제 때 가득 찬 것으로 봄. 원격 채우기가 생겨서 뺌)"""
    count = None
    if tray_stock.enabled():
        count = tray_stock.get()
        if count <= 0:
            raise tray_stock.WaitForRefill(tray_stock.describe(count))
        stack_no, floor, TRAY["pickup_slots"][1] = tray_stock.pick_pose(count)
        log(TRAY["port"], f"빈 트레이 {count}/{tray_stock.TOTAL} 남음 → 더미{stack_no} {floor}층 집기")
    if not (TRAY["pickup_slots"][1] and TRAY["place_slots"][1]):
        log(TRAY["port"], "트레이 좌표가 아직 없음 (pose_tray.json) → 실패 처리")
        return False
    ensure_connected({"tray": TRAY})
    ok = bool(_loop.run_until_complete(arm_set(_arm_api, TRAY)).get(1))
    if count is not None:
        if ok:
            tray_stock.used(order_id)  # 벨트에 놓고 빠진 것까지 확인됐을 때만 뺌 (못 집었으면 그 자리에 그대로 있음)
        log(TRAY["port"], tray_stock.describe(tray_stock.cached()))
    return ok


def run_keycap_arms(order_id, on_result=None):
    """keycap_reached 때 호출. 두 세트 로봇팔로 키캡 4개를 옮김. {1~4: bool} 반환.
    on_result(keycap번호, 성공여부) 를 주면 키캡 하나 끝날 때마다 바로 불러줌 (그때그때 보고)."""
    ensure_connected(SETS)
    return _loop.run_until_complete(all_arms(_arm_api, on_result))


# =====================================================================
# 단독 테스트 — 원본 two_sets_full.py 처럼 한 번 돌리고 결과 출력
# =====================================================================

def run_one_set(set_key):
    """단독 테스트용: 한 세트만 연결해서 컨베이어 + 픽앤플레이스 1회."""
    cfg = SETS[set_key]
    connect({set_key: cfg})
    try:
        res = _loop.run_until_complete(run_set(_arm_api, _box_api, cfg))
        log("DONE", f"컨베이어: {res['conveyor']}")
        log("DONE", f"로봇팔:   {res['pack']}")
    finally:
        disconnect()


if __name__ == "__main__":
    import sys
    from worker_lock import WorkerLock
    with WorkerLock():  # 게이트웨이가 켜져 있으면 같은 로봇을 동시에 움직이지 않게 중단
        if len(sys.argv) > 1 and sys.argv[1] in SETS:  # py -3.9 dobot_control.py set1 → 세트1만
            run_one_set(sys.argv[1])
            sys.exit()
        connect()
        try:
            conveyor_result = run_keycap_conveyor("standalone_test")
            pack_result = run_keycap_arms("standalone_test") if all(conveyor_result.values()) else "벨트 실패 → 팔 안 움직임"
            log("DONE", f"컨베이어: {conveyor_result}")
            log("DONE", f"로봇팔:   {pack_result}")
        finally:
            disconnect()
