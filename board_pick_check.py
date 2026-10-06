# -*- coding: utf-8 -*-
"""
픽업 위치 일괄 확인 - 흡착 없이 색별 픽업 위치로 이동만 해서 눈으로 확인하는 도구.
입력 없이 끝까지 자동으로 돎 (각 위치에서 DWELL_SEC 동안 멈춤).

사용: python board_pick_check.py [색=재고 ...] [줄/개수] [멈춤초]
  python board_pick_check.py                  -> 재고 기본값(DEFAULT_STOCK)으로 확인, 3초
  python board_pick_check.py r=1 g=20 b=1 y=1 -> 색별 재고 지정 (0이면 그 색 건너뜀)
  python board_pick_check.py 3/3 5            -> 4색 모두 줄3/3개 자리, 5초
  재고 지정 시 위치는 메인과 같은 계산(compute_pick_pose) - 그 재고일 때 메인이 집으러 가는 자리

경로: HOME -> 이동 높이로 픽업 위 -> 하강(멈춤) -> 이동 높이로 상승 -> 다음 색 ... -> HOME
  이동 높이 = (이번에 갈 자리 중 제일 높은 z, 트레이 z 중 큰 값) + SAFE_LIFT_MM - 모든 이동 공통
  (흡착 안 하니까 메인보다 보수적으로 - 높은 스택 위를 지나가도 안 부딪히게)

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
    pick_pose_for, compute_pick_pose, wrist_of, r_for, arc_midpoint, needs_midpoint, _wrap_deg,
)

COLOR_ORDER = ["red", "green", "blue", "yellow"]
COLOR_KEYS = {"r": "red", "g": "green", "b": "blue", "y": "yellow",
              "red": "red", "green": "green", "blue": "blue", "yellow": "yellow"}
DEFAULT_STOCK = {"red": 3, "green": 20, "blue": 3, "yellow": 3}

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


def parse_args(argv):
    """반환: (targets, dwell). targets = [(color, label, (x, y, z, r)), ...]"""
    stock = dict(DEFAULT_STOCK)
    row_cnt = None
    dwell = 3.0
    for a in argv:
        if "=" in a:
            k, v = a.split("=", 1)
            color = COLOR_KEYS.get(k.strip().lower())
            if color is None:
                raise SystemExit(f"색 '{k}' 잘못됨 - r/g/b/y")
            stock[color] = int(v)
        elif "/" in a:
            row, cnt = (int(v) for v in a.split("/"))
            if not (0 <= row < NUM_ROWS and 1 <= cnt <= ROW_CAPACITY):
                raise SystemExit(f"줄 0~{NUM_ROWS - 1}, 개수 1~{ROW_CAPACITY}")
            row_cnt = (row, cnt)
        else:
            dwell = float(a)

    targets = []
    for color in COLOR_ORDER:
        if row_cnt:
            targets.append((color, f"줄{row_cnt[0]}/{row_cnt[1]}개", pick_pose_for(color, *row_cnt)))
        elif stock[color] > 0:
            targets.append((color, f"재고 {stock[color]}", compute_pick_pose(color, stock[color])))
    return targets, dwell


def main():
    targets, dwell = parse_args(sys.argv[1:])
    if not targets:
        raise SystemExit("확인할 자리가 없음 (재고 전부 0)")
    travel_z = max(max(p[2] for _, _, p in targets), PLACE_XYZ[2]) + SAFE_LIFT_MM
    print(f"이동 높이 {travel_z:.1f} / 멈춤 {dwell:.0f}초", flush=True)

    print("연결:", loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))), flush=True)
    try:
        hx, hy, hz = HOME_XYZ
        move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="HOME")
        cur = HOME_XYZ

        for color, label, (px, py, pz, pr) in targets:
            wrist = wrist_of(px, py, pr)
            print(f"\n=== {color} {label}: ({px:.1f},{py:.1f},{pz:.1f}) R={pr:.1f}", flush=True)
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
