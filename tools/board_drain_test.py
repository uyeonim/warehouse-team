# -*- coding: utf-8 -*-
"""
재고 소진 순서 테스트 - 한 색을 재고 N(기본 20)부터 1까지 차례로 집어서 트레이에 놓음.
집는 자리는 메인과 같은 compute_pick_pose (층별: 0/5 1/5 2/5 3/5 0/4 ... 3/1).
트레이에 놓을 때마다 멈춤 - 사람이 치우고 엔터 치면 다음 보드.

사용: python tools/board_drain_test.py [색] [시작 재고]
  python tools/board_drain_test.py green       -> green 20개부터 1까지
  python tools/board_drain_test.py green 12    -> green 12부터 이어서

동작은 메인 pick_and_place와 같은 순서 (카메라 확인 / 서버 보고는 안 함 - 서버 재고 안 바뀜):
  이동 높이로 픽업 위 -> 하강 -> 흡착 ON -> 상승 -> (중간점) -> 트레이 위 -> 색별 놓기 R 회전
  -> 하강 -> 흡착 OFF -> 상승 -> 손목 0도 -> [엔터 대기] -> 다음 픽업
  이동 높이 = travel_z_for() - 꽉 찼을 때 제일 높은 자리 기준 고정 (메인과 동일)
픽업/트레이 하강 도착 실패 시: 흡착 끄고 상승 후 재시도(r) / 건너뛰기(s) / 종료(q) 선택.

!! 메인(main_board_robot_arm.py) 끄고 실행할 것 - 같은 팔에 명령이 섞임
"""

import asyncio
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # 루트의 board_config.py
from board_config import (
    DOBOT_PORT, BOARD_COLORS, TOTAL_CAPACITY, PICK_POSE_ROWS, BOARD_THICKNESS_MM, HOME_XYZ, HOME_WRIST_DEG, PLACE_XYZ, SAFE_LIFT_MM,
    PLACE_R_OFFSET_DEG_BY_COLOR,
    MOVE_TIMEOUT_SEC, MOVE_POLL_INTERVAL_SEC, PRECISION_SETTLE_SEC,
    MOVE_STABLE_MM, MOVE_STABLE_DEG, MOVE_RESEND_AFTER_SEC, MOVE_MAX_RESEND,
    POSITION_TOLERANCE_MM, POSITION_TOLERANCE_DEG,
    SUCTION_SETTLE_DELAY, RELEASE_SETTLE_DELAY,
    compute_pick_pose, travel_z_for, wrist_of, r_for, place_r_for, arc_midpoint, needs_midpoint, _wrap_deg,
)

COLOR_KEYS = {"r": "red", "g": "green", "b": "blue", "y": "yellow"}

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
arm_api = DobotlinkAdapter(RPCClient(), is_sync=False).MagicianLite


async def safe_call(coro, timeout_sec=5):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def get_pose():
    return loop.run_until_complete(safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False)))


def _matches(actual, target):
    tx, ty, tz, tr = target
    try:
        return (abs(actual["x"] - tx) <= POSITION_TOLERANCE_MM
                and abs(actual["y"] - ty) <= POSITION_TOLERANCE_MM
                and abs(actual["z"] - tz) <= POSITION_TOLERANCE_MM
                and abs(_wrap_deg(actual["r"] - tr)) <= POSITION_TOLERANCE_DEG)
    except (KeyError, TypeError):
        return False


def _is_still(prev, cur):
    try:
        return (abs(cur["x"] - prev["x"]) <= MOVE_STABLE_MM
                and abs(cur["y"] - prev["y"]) <= MOVE_STABLE_MM
                and abs(cur["z"] - prev["z"]) <= MOVE_STABLE_MM
                and abs(_wrap_deg(cur["r"] - prev["r"])) <= MOVE_STABLE_DEG)
    except (KeyError, TypeError):
        return False


def move_to(x, y, z, r, label="", settle_sec=0.0):
    """메인과 같은 이동 방식 - 도착+멈춤 확인, 멈춘 채 못 가면 재전송. 반환: 도착 여부."""
    def send():
        return loop.run_until_complete(safe_call(
            arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0, x=x, y=y, z=z, r=r, isQueued=False)))

    started = time.time()
    res = send()
    actual, arrived, resends, prev = None, False, 0, None
    still_since = time.time()
    while time.time() - started < MOVE_TIMEOUT_SEC:
        time.sleep(MOVE_POLL_INTERVAL_SEC)
        actual = get_pose()
        if not isinstance(actual, dict):
            continue
        if prev is None or not _is_still(prev, actual):
            still_since = time.time()
        elif _matches(actual, (x, y, z, r)):
            arrived = True
            break
        elif time.time() - still_since >= MOVE_RESEND_AFTER_SEC:
            if resends >= MOVE_MAX_RESEND:
                break
            resends += 1
            res = send()
            still_since = time.time()
        prev = actual
    if arrived and settle_sec > 0:
        time.sleep(settle_sec)
    elapsed = time.time() - started
    status = f"도착 {elapsed:.1f}초" if arrived else f"!! 도착 실패({elapsed:.1f}초)"
    if resends:
        status += f", 재전송 {resends}회"
    pos = (f"({actual['x']:.1f},{actual['y']:.1f},{actual['z']:.1f},{actual['r']:.1f})"
           if isinstance(actual, dict) else str(actual))
    print(f"  [{label}] {res} {status} 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}) 실제={pos}", flush=True)
    return arrived


def travel(from_xyz, to_xyz, wrist, label):
    if needs_midpoint(from_xyz[:2], to_xyz[:2]):
        mx, my = arc_midpoint(from_xyz[:2], to_xyz[:2])
        move_to(mx, my, max(from_xyz[2], to_xyz[2]), r_for(mx, my, wrist), label=f"{label} - 중간점")
    tx, ty, tz = to_xyz
    return move_to(tx, ty, tz, r_for(tx, ty, wrist), label=label)


def suck(on):
    # OFF = 배기 -> RELEASE_SETTLE_DELAY -> 펌프 정지(enable=False) - 메인 suck()과 동일 (배기만 두면 작동음 남음)
    res = loop.run_until_complete(safe_call(
        arm_api.SetEndEffectorSuctionCup(portName=DOBOT_PORT, enable=True, on=on, isQueued=False)))
    if not on:
        time.sleep(RELEASE_SETTLE_DELAY)
        stop = loop.run_until_complete(safe_call(
            arm_api.SetEndEffectorSuctionCup(portName=DOBOT_PORT, enable=False, on=False, isQueued=False)))
        res = f"배기={res}, 펌프 정지={stop}"
    print(f"  흡착 {'ON' if on else 'OFF'}: {res}", flush=True)


def ask_on_fail(what):
    while True:
        a = input(f"{what} 도착 실패 - 재시도(r) / 건너뛰기(s) / 종료(q): ").strip().lower()
        if a in ("r", "s", "q"):
            return a


def pick_and_place_one(color, count, cur_xyz, cur_wrist):
    """보드 1개 픽업 -> 트레이에 놓기. 반환: (결과 'ok'|'skip'|'retry'|'quit', 현재 위치, 현재 손목)."""
    px, py, pz, pr = compute_pick_pose(color, count)
    wrist = wrist_of(px, py, pr)
    place_x, place_y, place_z = PLACE_XYZ
    travel_z = travel_z_for(pz)

    # 현재 위치에서 이동 높이로 -> 픽업 위 -> 하강
    move_to(cur_xyz[0], cur_xyz[1], max(cur_xyz[2], travel_z), r_for(cur_xyz[0], cur_xyz[1], cur_wrist),
            label="상승")
    travel((cur_xyz[0], cur_xyz[1], max(cur_xyz[2], travel_z)), (px, py, travel_z), wrist, "픽업 위치 위")
    if not move_to(px, py, pz, r_for(px, py, wrist), label="픽업 하강", settle_sec=PRECISION_SETTLE_SEC):
        move_to(px, py, travel_z, r_for(px, py, wrist), label="상승")
        a = ask_on_fail("픽업 하강")
        return {"r": "retry", "s": "skip", "q": "quit"}[a], (px, py, travel_z), wrist

    suck(True)
    time.sleep(SUCTION_SETTLE_DELAY)
    move_to(px, py, travel_z, r_for(px, py, wrist), label="들어올리기")

    # 트레이 위 -> 색별 놓기 R 회전 -> 하강
    travel((px, py, travel_z), (place_x, place_y, travel_z), wrist, "트레이 위")
    place_r = place_r_for(color)
    move_to(place_x, place_y, travel_z, place_r,
            label=f"놓기 R 회전 ({PLACE_R_OFFSET_DEG_BY_COLOR[color]:+.0f}도)")
    if not move_to(place_x, place_y, place_z, place_r, label="트레이 하강", settle_sec=PRECISION_SETTLE_SEC):
        suck(False)
        move_to(place_x, place_y, travel_z, place_r, label="상승")
        move_to(place_x, place_y, travel_z, r_for(place_x, place_y, HOME_WRIST_DEG), label="손목 0도")
        a = ask_on_fail("트레이 하강 (보드는 그 높이에서 떨어졌을 수 있음)")
        return {"r": "retry", "s": "skip", "q": "quit"}[a], (place_x, place_y, travel_z), HOME_WRIST_DEG

    suck(False)
    move_to(place_x, place_y, travel_z, place_r, label="상승")
    move_to(place_x, place_y, travel_z, r_for(place_x, place_y, HOME_WRIST_DEG), label="손목 0도")
    return "ok", (place_x, place_y, travel_z), HOME_WRIST_DEG


def main():
    color = "green"
    start = TOTAL_CAPACITY
    if len(sys.argv) > 1:
        color = COLOR_KEYS.get(sys.argv[1].lower(), sys.argv[1].lower())
    if len(sys.argv) > 2:
        start = int(sys.argv[2])
    if color not in BOARD_COLORS or not (1 <= start <= TOTAL_CAPACITY):
        raise SystemExit(f"색은 {BOARD_COLORS}, 시작 재고는 1~{TOTAL_CAPACITY}")

    print(f"[{color}] 재고 {start} -> 1 순서대로 픽업 테스트. 놓을 때마다 엔터 대기.", flush=True)
    print("연결:", loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))), flush=True)
    try:
        hx, hy, hz = HOME_XYZ
        move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="HOME")
        cur_xyz, cur_wrist = HOME_XYZ, HOME_WRIST_DEG

        count = start
        while count >= 1:
            px, py, pz, _ = compute_pick_pose(color, count)
            row = next(i for i, p in enumerate(PICK_POSE_ROWS[color]) if p[0] == px)
            layer = round((pz - PICK_POSE_ROWS[color][row][2]) / BOARD_THICKNESS_MM)
            print(f"\n=== [{color}] 재고 {count}: {row}/{layer} ({px:.1f},{py:.1f},{pz:.1f}) ===", flush=True)

            result, cur_xyz, cur_wrist = pick_and_place_one(color, count, cur_xyz, cur_wrist)
            if result == "quit":
                break
            if result == "retry":
                continue
            if result == "ok":
                a = input(f">>> 재고 {count} 보드 트레이에 놓음 - 치우고 엔터 (q=종료): ").strip().lower()
                if a == "q":
                    break
            count -= 1

        print("\nHOME 복귀", flush=True)
        travel(cur_xyz, HOME_XYZ, cur_wrist, "HOME 복귀")
    finally:
        suck(False)
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        print("연결 해제", flush=True)


if __name__ == "__main__":
    main()
