# -*- coding: utf-8 -*-
"""
창고팀 통신 서버 (gcloud 서버에서 실행)

cnt_process 알림을 받아서, 창고 차례인 status 가 오면
장비 동작 → 담당 컨테이너에 결과(done/failed) 보고.

지금은 하드웨어 없이 통신만 확인하는 단계라 "장비 동작" 구역은 가짜(항상 성공)입니다.
실제 장비를 붙일 때는 그 구역의 함수 안만 바꾸면 되고, 보고 형식은 그대로입니다.

  받은 status          장비 동작               보고 컨테이너 ← 내용
  assigned            빈 트레이 투입 (기본 꺼짐) cnt_robot_arm/cnt_tray_robot_arm   ← {order_id, status}
  keycap_dispensed    키캡 벨트 이송           cnt_conveyor/cnt_keycap_conveyor   ← {order_id, keycap1~4}
  keycap_reached      키캡 4개 트레이 적재      cnt_robot_arm/cnt_keycap_robot_arm ← {order_id, keycapN} ×4건
  keycap_packed       트레이 벨트 section1     cnt_conveyor/cnt_tray_conveyor     ← {order_id, section1}
  tray_reached        보드 투입                cnt_robot_arm/cnt_board_robot_arm  ← {order_id, board}
  board_mismatched    보드 재투입 (불출 불일치)   + 성공 시 cnt_warehouse/cnt_board_dispense ← {order_id, board}
  board_packed        트레이 벨트 section2     cnt_conveyor/cnt_tray_conveyor     ← {order_id, section2}

  창고(컨베이어·로봇팔) 차례가 아닌 status 는 기다리기만 합니다.
    keycap_mismatched → 키캡 카트리지(아두이노) 재배출 차례. 컨베이어는 keycap_dispensed 가 올 때까지 대기.
  불출 검사(주문 ↔ 배출 대조)는 플랫폼이 cnt_*_dispense 를 보고 판정합니다.

.env 설정
  HARDWARE           fake(기본) → 가짜 장비 / dobot → 키캡 벨트·로봇팔 2세트를 dobot_control.py 로 실제 제어
                     (dobot 은 USB 가 연결된 PC 에서 pc_gateway.py 로 실행. 트레이·보드는 2세트 범위 밖이라 계속 가짜)
  REACT_ASSIGNED     1 이면 assigned(트레이 로봇팔 빈 트레이 투입)에 반응                  기본 0
                     트레이 로봇팔이 아직 없고, 플랫폼은 이 보고를 기다리지 않으므로 꺼둠
  FAKE_UNBUILT       1 이면 아직 없는 장비(트레이 벨트·보드 로봇팔) 단계에 가짜 done 보고   기본 1
                     플랫폼이 section1·board·section2 보고를 기다리므로, 끄면 흐름이 멈춤
    장비별로 따로 정하기 (비워두면 FAKE_UNBUILT 값을 따름) — 다른 팀이 보고하는 컨테이너는 0 으로 꺼서 중복 방지
    FAKE_TRAY_CONVEYOR   cnt_tray_conveyor  section1 (keycap_packed → tray_reached)
    FAKE_TRAY_SECTION2   cnt_tray_conveyor  section2 (board_packed → pickup_reached). 비우면 FAKE_TRAY_CONVEYOR 를 따름
    FAKE_BOARD_ARM       cnt_board_robot_arm board
    FAKE_BOARD_DISPENSE  cnt_board_dispense  board 색
    ※ 2026-09-14 담당 범위 확정: 우리는 tray_reached 를 만드는 section1 까지. 보드·section2 는 0.
  REPORT_ROOT        비어 있음 → 진짜 컨테이너에 보고 / 폴더 이름 → 그 폴더 아래에 보고 (연습용)
  CHECK_GRIP_IR      1 이면 집은 뒤 광전센서로 집기 확인 (센서 연결 후)          기본 0
  CHECK_BOARD_COLOR  1 이면 보드 위치 컬러센서로 실제 보드 색 확인               기본 0
  MANUAL_FIX_DONE    1 이면 fault 가 풀린 뒤 keycap_reached 를 "사람이 조치함"으로 보고 (done)  기본 1
                     0 으로 두면 저장된 결과(대개 failed)를 그대로 보고 → 같은 fault 가 반복될 수 있음
  FORCE_FAIL         테스트용. 일부러 failed 로 보고할 부분 (belt_keycap2 / arm_keycap2 / keycap2 / section1 ...)
                     장비 고장 없이 fault 흐름(우리 failed → 플랫폼 fault → 정지 → 해제)을 확인할 때만 사용

실행:  python3 -u warehouse_server.py
"""

import json
import os

from flask import Flask, jsonify, request

import mobius


def _flag(name, default="0"):
    return (os.environ.get(name) or default).strip() == "1"


HARDWARE = os.environ.get("HARDWARE", "fake").strip().lower()
if HARDWARE == "dobot":
    import dobot_control as hw  # DobotRPC 가 있는 PC 에서만
else:
    hw = None

REPORT_ROOT = os.environ.get("REPORT_ROOT", "").strip("/")
REACT_ASSIGNED = _flag("REACT_ASSIGNED")
FAKE_UNBUILT = _flag("FAKE_UNBUILT", "1")  # 아래 장비별 설정을 비워두면 이 값을 따름
FAKE_TRAY_CONVEYOR = _flag("FAKE_TRAY_CONVEYOR", "1" if FAKE_UNBUILT else "0")
FAKE_TRAY_SECTION2 = _flag("FAKE_TRAY_SECTION2", "1" if FAKE_TRAY_CONVEYOR else "0")
FAKE_BOARD_ARM = _flag("FAKE_BOARD_ARM", "1" if FAKE_UNBUILT else "0")
FAKE_BOARD_DISPENSE = _flag("FAKE_BOARD_DISPENSE", "1" if FAKE_UNBUILT else "0")
CHECK_GRIP_IR = _flag("CHECK_GRIP_IR")
CHECK_BOARD_COLOR = _flag("CHECK_BOARD_COLOR")
MANUAL_FIX_DONE = _flag("MANUAL_FIX_DONE", "1")
# 1 이면 보고를 실제로 올리지 않고 화면에만 출력 (우리끼리 흐름 확인용). 평소에는 0
DRY_RUN = _flag("DRY_RUN")
# 테스트용: 일부러 failed 로 보고할 부분. 장비는 정상이어도 실패로 보고함
#   belt_keycap2 = 키캡 벨트 보고만 / arm_keycap2 = 키캡 로봇팔 보고만 / keycap2 = 둘 다
#   section1, section2 = 트레이 벨트
FORCE_FAIL = {x.strip() for x in os.environ.get("FORCE_FAIL", "").split(",") if x.strip()}
PORT = 5000

# 보고 컨테이너 (명세의 이름 그대로)
TRAY_ARM = "cnt_robot_arm/cnt_tray_robot_arm"
KEYCAP_CONVEYOR = "cnt_conveyor/cnt_keycap_conveyor"
KEYCAP_ARM = "cnt_robot_arm/cnt_keycap_robot_arm"
TRAY_CONVEYOR = "cnt_conveyor/cnt_tray_conveyor"
BOARD_ARM = "cnt_robot_arm/cnt_board_robot_arm"
BOARD_DISPENSE = "cnt_warehouse/cnt_board_dispense"

SHELVES = {1: "EI", 2: "NS", 3: "TF", 4: "PJ"}  # keycap1~4 담당 선반 = MBTI 1~4번째 글자

app = Flask(__name__)

last_seq = None
handled_by_order = {}  # order_id -> 보고까지 끝낸 (status, retry)
conveyor_results = {}  # order_id -> 벨트 실행 결과. 보고 전송만 실패했을 때 재시도에서 장비를 다시 돌리지 않도록 보관
fault_seen = set()     # fault 로 멈췄던 주문. 해제 뒤 keycap_reached 를 "사람이 고침"으로 처리하는 데 씀
arm_reported = {}      # order_id -> {슬롯: 성공여부}. fault 해제 뒤 실패했던 키캡만 다시 보고하려고 기억
forced_used = set()    # (order_id, FORCE_FAIL 이름) — 이미 일부러 실패시킨 것 (주문당 한 번만)
arm_results = {}       # order_id -> {슬롯: 로봇팔 결과}. 로봇팔은 주문당 한 번만 움직이도록 보관


def result(ok):
    return "done" if ok else "failed"


def order_keycaps(order):
    """주문의 키캡 4개. keycap "ENFP" + colors [r,y,b,g] → {1: "E_r", 2: "N_y", 3: "F_b", 4: "P_g"}"""
    letters = order.get("keycap") or ""
    colors = order.get("colors") or []
    return {i + 1: f"{letter}_{color}" for i, (letter, color) in enumerate(zip(letters, colors))}


def report(path, con):
    full_path = f"{REPORT_ROOT}/{path}" if REPORT_ROOT else path
    if DRY_RUN:  # 우리끼리 흐름만 확인할 때. Mobius 에 아무것도 올리지 않음
        print(f"    → [모의] 보고 안 함 {full_path}: {json.dumps(con, ensure_ascii=False)}")
        return
    mobius.create_cin(full_path, con)
    print(f"    → 보고 {full_path}: {json.dumps(con, ensure_ascii=False)}")


# =====================================================================
# 장비 동작 — HARDWARE=dobot 이면 키캡 벨트·로봇팔은 dobot_control.py 가 실제 제어,
#   나머지(트레이·보드)와 HARDWARE=fake 는 가짜(항상 성공).
#   반환값 True = 성공, False = 실패. 센서 확인은 .env 플래그가 1 일 때만 동작.
# =====================================================================

def forced_fail(order_id, *names):
    """테스트용. .env 의 FORCE_FAIL 에 적힌 이름 중 하나라도 맞으면 True (실패로 보고).
    주문마다 한 번만 실패시킴 → fault 해제 뒤 다시 실행할 때는 정상(done). 안 그러면 failed→fault 가 무한 반복."""
    for name in names:
        if name in FORCE_FAIL:
            if (order_id, name) in forced_used:
                print(f"    (FORCE_FAIL {name}: 이 주문은 이미 한 번 실패로 보냄 → 이번엔 정상 처리)")
                return False
            forced_used.add((order_id, name))
            print(f"    ✗ {name}: FORCE_FAIL 설정이라 일부러 failed 로 보고 (장비 상태와 무관)")
            return True
    return False


def read_grip_ir(slot):
    """[센서 자리] 집은 뒤 픽업 위치 광전센서가 꺼졌으면(=키캡 없음) True."""
    return True


def read_board_color():
    """[센서 자리] 보드 위치 컬러센서 값. 지금은 읽을 수 없으니 None."""
    return None


def load_empty_tray(order_id):
    """빈 트레이를 쌓아둔 곳에서 집어 벨트에 올림 (status=assigned)."""
    if hw:
        print("    [장비·Dobot] 트레이 로봇팔: 빈 트레이 집어서 벨트에 올리기")
        return hw.run_tray_arm(order_id)
    print("    [장비·가짜] 트레이 로봇팔: 빈 트레이 투입")
    return True


def run_keycap_conveyor(order_id):
    """벨트 이송. 키캡별 도착 여부 {1: True, 2: True, 3: True, 4: True} (세트1=1·2, 세트2=3·4)"""
    if hw:
        print("    [장비·Dobot] 세트1·2 벨트 구동 (로봇팔은 keycap_reached 때)")
        arrived = hw.run_keycap_conveyor(order_id)
    else:
        print("    [장비·가짜] 키캡 벨트 1·2 동시 구동")
        arrived = {1: True, 2: True, 3: True, 4: True}
    return {i: ok and not forced_fail(order_id, f"keycap{i}", f"belt_keycap{i}") for i, ok in arrived.items()}


def check_packed(order_id, slot, arrived):
    """로봇팔 결과에 테스트용 강제 실패·센서 확인을 더해 최종 성공 여부."""
    if forced_fail(order_id, f"keycap{slot}", f"arm_keycap{slot}"):
        return False
    if arrived and CHECK_GRIP_IR and not read_grip_ir(slot):
        print(f"    ✗ keycap{slot} 집기 실패 (광전센서에 키캡이 남아 있음)")
        return False
    return arrived


def run_keycap_arms(order_id, keycaps, on_result):
    """키캡 로봇팔로 4개를 트레이 선반 칸에 배치. 하나 끝날 때마다 on_result(슬롯, 성공여부) 호출."""
    if hw:
        print("    [장비·Dobot] 두 세트 로봇팔 실행 — 키캡 하나 놓을 때마다 바로 보고")
        hw.run_keycap_arms(order_id, on_result)
    else:
        for slot in SHELVES:
            print(f"    [장비·가짜] 키캡 로봇팔: keycap{slot}({SHELVES[slot]} 선반, {keycaps.get(slot)}) 픽업 → 배치")
            on_result(slot, True)


def run_tray_conveyor(order_id, section):
    print(f"    [장비·가짜] 트레이 벨트 {section} 구동 (2세트 범위 밖)")
    return not forced_fail(order_id, section)


def place_board(order_id, board):
    """보드 투입. (성공 여부, 실제 투입된 보드 색) 반환.
    컬러센서가 꺼져 있으면 실제 색을 알 수 없으므로 주문 색을 그대로 씀."""
    print(f"    [장비·가짜] 보드 로봇팔: {board} 보드 투입 (2세트 범위 밖)")
    placed = board
    if CHECK_BOARD_COLOR:
        sensed = read_board_color()
        if sensed is not None:
            placed = sensed
    return True, placed


# =====================================================================
# status 별 처리 — 장비 동작 결과를 명세 형식 그대로 보고
# =====================================================================

def on_assigned(order_id, order):
    report(TRAY_ARM, {"order_id": order_id, "status": result(load_empty_tray(order_id))})


def on_keycap_dispensed(order_id, order):
    if order_id in conveyor_results:
        print("    (장비는 이미 실행함 — 보고만 재시도)")
    else:
        conveyor_results[order_id] = run_keycap_conveyor(order_id)
    arrived = conveyor_results[order_id]
    con = {"order_id": order_id}
    con.update({f"keycap{i}": result(arrived.get(i, False)) for i in SHELVES})
    # 실패한 벨트 구간을 알려줌 (keycap1·2 = 세트1 = 01, keycap3·4 = 세트2 = 02).
    # 안 보내면 플랫폼이 무조건 01 로 처리해서, 세트2 가 멈춰도 keycap_01_belt_jammed 로 잘못 뜸.
    failed = [i for i in SHELVES if not arrived.get(i, False)]
    if failed:
        con["section"] = "01" if min(failed) <= 2 else "02"
    report(KEYCAP_CONVEYOR, con)
    del conveyor_results[order_id]  # 보고 성공 → 다음에 keycap_dispensed 가 다시 오면 새로 실행


def on_keycap_reached(order_id, order):
    # fault 로 멈췄다가 풀린 주문: 로봇팔은 이미 한 번 움직였고 다시 움직이지 않으므로,
    # 저장된 failed 를 또 보고하면 플랫폼이 같은 fault 를 다시 달아 무한 반복됨.
    # fault 해제 = 사람이 조치 완료 라는 뜻이므로 done 으로 보고 (MANUAL_FIX_DONE=0 이면 이 처리 끔).
    # 이때 **실패했던 키캡만** 다시 보고함. 이미 done 을 보낸 키캡까지 또 보내면
    # 플랫폼이 중복으로 보고 무시할 수 있어 stage_timeout 이 남 (2026-09-17 ord_272b001a 사례).
    manual_fix = MANUAL_FIX_DONE and order_id in fault_seen
    if manual_fix:
        # 4건 전부 다시 보냄. 플랫폼은 fault 가 걸려 있는 동안 받은 로봇암 보고를 버리므로
        # (2026-10-02 구조 문서 3장 공통 가드), 실패한 것만 보내면 나머지가 영영 안 모여 stage_timeout 이 남.
        todo = list(SHELVES)
        print(f"    (fault 해제됨 → 사람이 조치한 것으로 보고: keycap{todo} done 재보고, 로봇팔은 안 움직임)")
        for i in todo:
            report(KEYCAP_ARM, {"order_id": order_id, f"keycap{i}": "done"})
            arm_reported.setdefault(order_id, {})[i] = True
        return

    if order_id in fault_seen:
        arm_reported.pop(order_id, None)  # MANUAL_FIX_DONE=0 로 fault 뒤 다시 온 경우: 저장된 결과를 다시 보고

    def send(slot, arrived):
        ok = check_packed(order_id, slot, arrived)
        report(KEYCAP_ARM, {"order_id": order_id, f"keycap{slot}": result(ok)})
        arm_reported.setdefault(order_id, {})[slot] = ok

    # 로봇팔은 주문당 한 번만 움직임. 보고 전송만 실패해서 재시도할 때는 저장된 결과로 못 보낸 것만 다시 보냄
    if order_id not in arm_results:
        arm_results[order_id] = {}

        def on_result(slot, arrived):  # 키캡 하나 놓을 때마다 바로 보고 (4개 다 끝날 때까지 안 기다림)
            arm_results[order_id][slot] = arrived
            try:
                send(slot, arrived)
            except Exception as e:
                print(f"    ✗ keycap{slot} 보고 전송 실패 → 로봇팔 다 끝난 뒤 다시 보냄: {e}")

        run_keycap_arms(order_id, order_keycaps(order), on_result)

    for slot in SHELVES:  # 못 보낸 것 마저 (여기서 실패하면 예외 → 다음 폴링 때 재시도, 로봇팔은 안 움직임)
        if slot not in arm_reported.get(order_id, {}):
            send(slot, arm_results[order_id].get(slot, False))


def on_keycap_packed(order_id, order):
    report(TRAY_CONVEYOR, {"order_id": order_id, "section1": result(run_tray_conveyor(order_id, "section1"))})


def on_board_turn(order_id, order):
    """tray_reached(첫 투입) · board_mismatched(재투입) 공통."""
    ok, placed = place_board(order_id, order.get("board"))
    if FAKE_BOARD_ARM:
        report(BOARD_ARM, {"order_id": order_id, "board": result(ok)})
    else:
        print("    (cnt_board_robot_arm 은 보드 로봇암 팀이 보고 — 건너뜀)")
    if ok and FAKE_BOARD_DISPENSE:  # 실제로 투입한 보드만 배출 기록 → 플랫폼이 주문 색과 대조
        report(BOARD_DISPENSE, {"order_id": order_id, "board": placed})


def on_board_packed(order_id, order):
    report(TRAY_CONVEYOR, {"order_id": order_id, "section2": result(run_tray_conveyor(order_id, "section2"))})


# 조립된 장비(키캡 2세트) 단계 — 항상 반응
HANDLERS = {
    "keycap_dispensed": on_keycap_dispensed,
    "keycap_reached": on_keycap_reached,
}

WAITING = {  # 창고(컨베이어·로봇팔) 차례는 아니지만 로그로 남길 status
    "keycap_mismatched": "키캡 카트리지 재배출 중 — 컨베이어 대기",
}

# 아직 없는 장비 단계 — 설정에 따라 가짜 보고 또는 건너뜀
if REACT_ASSIGNED:
    HANDLERS["assigned"] = on_assigned
else:
    WAITING["assigned"] = "트레이 로봇팔 없음 — 반응 안 함 (REACT_ASSIGNED=0)"

UNBUILT_HANDLERS = [  # (가짜 보고 켜짐, 담당 status, 끔 설정 이름)
    (FAKE_TRAY_CONVEYOR, {"keycap_packed": on_keycap_packed}, "FAKE_TRAY_CONVEYOR"),
    (FAKE_TRAY_SECTION2, {"board_packed": on_board_packed}, "FAKE_TRAY_SECTION2"),
    (FAKE_BOARD_ARM or FAKE_BOARD_DISPENSE, {"tray_reached": on_board_turn, "board_mismatched": on_board_turn},
     "FAKE_BOARD_ARM·FAKE_BOARD_DISPENSE"),
]
for _enabled, _handlers, _setting in UNBUILT_HANDLERS:
    if _enabled:
        HANDLERS.update(_handlers)
    else:
        for _status in _handlers:
            WAITING[_status] = f"장비 없음 — 반응 안 함 ({_setting}=0, 다른 팀이 보고하지 않으면 흐름이 여기서 멈춤)"


def settings_fault_summary():
    forced = ", ".join(sorted(FORCE_FAIL)) if FORCE_FAIL else "없음"
    dry = "  ※ 모의 모드(DRY_RUN): 보고를 실제로 올리지 않음" if DRY_RUN else ""
    return f"fault 설정: 사람 조치 시 done 보고={MANUAL_FIX_DONE}, 일부러 실패(FORCE_FAIL)={forced}{dry}"


def settings_summary():
    return (f"반응 설정: assigned={REACT_ASSIGNED}, 가짜 보고 트레이벨트section1={FAKE_TRAY_CONVEYOR} "
            f"section2={FAKE_TRAY_SECTION2} 보드로봇팔={FAKE_BOARD_ARM} 보드배출={FAKE_BOARD_DISPENSE}")


# =====================================================================
# 알림 처리
# =====================================================================

def extract_snapshot(body):
    """oneM2M 알림 {"m2m:sgn": {"nev": {"rep": {"m2m:cin": {"con": ...}}}}} 에서 con 꺼내기."""
    try:
        con = body["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"]
    except (KeyError, TypeError):
        return None
    if isinstance(con, str):
        try:
            con = json.loads(con)
        except json.JSONDecodeError:
            return None
    return con if isinstance(con, dict) else None


def handle_order(order_id, order):
    """주문 1건 처리. 보고 전송(HTTP)이 실패하면 False → 같은 사진이 다시 오면 재시도."""
    status = order.get("status")

    if order.get("fault"):
        print(f"[정지] {order_id} status={status} fault={order['fault']}")
        handled_by_order.pop(order_id, None)  # fault 가 풀리면 같은 status 로 다시 실행되도록
        conveyor_results.pop(order_id, None)
        fault_seen.add(order_id)
        return True

    handler = HANDLERS.get(status)
    if handler is None:
        if status in WAITING:
            print(f"[대기] {order_id} {status} — {WAITING[status]}")
        return True

    # 명세에는 재시도 횟수 필드가 없음. 플랫폼이 retry 를 넣어주면 같은 mismatched 도 재실행됨
    key = (status, order.get("retry"))
    if handled_by_order.get(order_id) == key:
        return True

    print(f"[{status}] {order_id} → 창고 차례")
    try:
        handler(order_id, order)
    except Exception as e:
        print(f"    ✗ 보고 전송 실패, 재시도 예정: {e}")
        return False
    handled_by_order[order_id] = key
    fault_seen.discard(order_id)  # 한 단계 정상 처리했으면 fault 흔적은 지움
    return True


def process_snapshot(snapshot):
    """cnt_process 사진 1장 처리. 결과: "duplicate" / "ok" / "retry" (서버·PC 게이트웨이 공용)"""
    global last_seq

    seq = snapshot.get("seq")
    if seq is not None and seq == last_seq:
        return "duplicate"  # 같은 사진 재전송

    orders = snapshot.get("orders") or {}
    all_ok = all([handle_order(order_id, order) for order_id, order in orders.items()])

    for memory in (handled_by_order, conveyor_results, arm_reported, arm_results):  # 끝난 주문 정리
        for order_id in list(memory):
            if order_id not in orders:
                del memory[order_id]
    for order_id in list(fault_seen):
        if order_id not in orders:
            fault_seen.discard(order_id)
    forced_used.difference_update({k for k in forced_used if k[0] not in orders})

    if all_ok:
        last_seq = seq  # 실패가 있으면 기록하지 않음 → 같은 seq 재전송 때 다시 처리
    return "ok" if all_ok else "retry"


def mark_as_handled(snapshot):
    """사진 속 주문들을 '이미 처리함'으로만 기록 (실행 안 함). PC 게이트웨이 시작 때 옛 사진에 반응하지 않도록."""
    global last_seq
    last_seq = snapshot.get("seq")
    for order_id, order in (snapshot.get("orders") or {}).items():
        handled_by_order[order_id] = (order.get("status"), order.get("retry"))


@app.route("/notify", methods=["POST"])
def notify():
    snapshot = extract_snapshot(request.get_json(force=True, silent=True) or {})
    if snapshot is None:
        print("[무시] 스냅샷이 아닌 알림 (구독 확인용 등)")
        return jsonify(result="ignored")
    return jsonify(result=process_snapshot(snapshot))


if __name__ == "__main__":
    print(f"창고 통신 서버 시작 — 보고 위치: {REPORT_ROOT or '진짜 컨테이너'}")
    print(settings_summary())
    print(f"센서 확인: 집기 IR={CHECK_GRIP_IR}, 보드 색={CHECK_BOARD_COLOR}")
    print(settings_fault_summary())
    app.run(host="0.0.0.0", port=PORT)
