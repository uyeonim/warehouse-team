# -*- coding: utf-8 -*-
"""
픽업 위치 일괄 확인 - 흡착 없이 색별 픽업 위치로 이동만 해서 눈으로 확인하는 도구.
입력 없이 끝까지 자동으로 돎 (각 위치에서 DWELL_SEC 동안 멈춤).

사용: python board_pick_check.py [줄/개수] [멈춤초]
  예) python board_pick_check.py 3/3 3     (기본값: 3/3, 3초)

경로는 메인 코드와 같은 규칙:
  HOME -> 이동 높이로 픽업 위 -> 하강(멈춤) -> 이동 높이로 상승 -> 다음 색 ... -> HOME
  이동 높이 = max(픽업 z, 트레이 z) + SAFE_LIFT_MM (메인 travel_z와 동일)

⚠️ 메인(main_board_robot_arm.py) 끄고 실행할 것 - 같은 팔에 명령이 섞임
"""

import asyncio
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

from board_config import (
    DOBOT_PORT, NUM_ROWS, ROW_CAPACITY, HOME_XYZ, HOME_WRIST_DEG, PLACE_XYZ, SAFE_LIFT_MM,
    MOVE_TIMEOUT_SEC, MOVE_POLL_INTERVAL_SEC,
    MOVE_STABLE_MM, MOVE_STABLE_DEG, MOVE_RESEND_AFTER_SEC, MOVE_MAX_RESEND,
    POSITION_TOLERANCE_MM, POSITION_TOLERANCE_DEG,
    pick_pose_for, wrist_of, r_for, arc_midpoint, needs_midpoint, _wrap_deg,
)

COLOR_ORDER = ["red", "green", "blue", "yellow"]

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


def move_to(x, y, z, r, label=""):
    """메인과 같은 이동 방식 - 도착+멈춤 확인, 멈춘 채 못 가면 재전송."""
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
    elapsed = time.time() - started
    status = f"도착 {elapsed:.1f}초" if arrived else f"[도착 실패 {elapsed:.1f}초]"
    if resends:
        status += f", 재전송 {resends}회"
    pos = (f"({actual['x']:.1f},{actual['y']:.1f},{actual['z']:.1f},{actual['r']:.1f})"
           if isinstance(actual, dict) else str(actual))
    print(f"[{label}] {res} {status} 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}) 실제={pos}", flush=True)
    return arrived


def travel(from_xyz, to_xyz, wrist, label):
    if needs_midpoint(from_xyz[:2], to_xyz[:2]):
        mx, my = arc_midpoint(from_xyz[:2], to_xyz[:2])
        move_to(mx, my, max(from_xyz[2], to_xyz[2]), r_for(mx, my, wrist), label=f"{label} - 중간점")
    tx, ty, tz = to_xyz
    return move_to(tx, ty, tz, r_for(tx, ty, wrist), label=label)


def main():
    row, cnt = 3, 3
    dwell = 3.0
    if len(sys.argv) > 1:
        row, cnt = (int(v) for v in sys.argv[1].split("/"))
    if len(sys.argv) > 2:
        dwell = float(sys.argv[2])
    if not (0 <= row < NUM_ROWS and 1 <= cnt <= ROW_CAPACITY):
        raise SystemExit(f"줄 0~{NUM_ROWS - 1}, 개수 1~{ROW_CAPACITY}")

    print("연결:", loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))), flush=True)
    try:
        hx, hy, hz = HOME_XYZ
        move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="HOME")
        cur = HOME_XYZ

        for color in COLOR_ORDER:
            px, py, pz, pr = pick_pose_for(color, row, cnt)
            wrist = wrist_of(px, py, pr)
            travel_z = max(pz, PLACE_XYZ[2]) + SAFE_LIFT_MM
            print(f"\n=== {color} 줄{row}/{cnt}개: ({px:.1f},{py:.1f},{pz:.1f}) R={pr:.1f}", flush=True)
            if cur != HOME_XYZ:
                move_to(cur[0], cur[1], travel_z, r_for(cur[0], cur[1], cur_wrist), label="상승")
            travel((cur[0], cur[1], travel_z), (px, py, travel_z), wrist, f"{color} 위로")
            ok = move_to(px, py, pz, r_for(px, py, wrist), label=f"{color} 픽업 위치")
            print(f">>> {color} 픽업 위치 {'도착' if ok else '도착 실패'} - {dwell:.0f}초 멈춤", flush=True)
            time.sleep(dwell)
            move_to(px, py, travel_z, r_for(px, py, wrist), label="상승")
            cur, cur_wrist = (px, py, travel_z), wrist

        travel(cur, HOME_XYZ, cur_wrist, "HOME 복귀")
    finally:
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        print("연결 해제", flush=True)


if __name__ == "__main__":
    main()
