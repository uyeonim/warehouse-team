# -*- coding: utf-8 -*-
"""
보드 집고 놓기 테스트 (대화형) - 원하는 색·줄·개수 자리에서 보드를 집어 트레이에 놓아 봄.
카메라 확인 / 서버 보고 / 재고 계산은 안 함 (재고·Mobius 안 건드림). 팔과 흡착만 움직임.

사용 (보드 main 을 끄고 실행 - 같은 팔을 씀. 팔이 실제로 움직임):
  python tools/board_pickplace_test.py

명령 (색: red green blue yellow 또는 r g b y / 줄: 0~3 / 개수: 그 줄에 쌓인 보드 수, 생략 = 5 = 꽉 찼을 때 맨 위):
  red 0           빨강 줄0 의 맨 위(5개 자리)에서 집어 트레이에 놓음
  green 2 3       초록 줄2 에 3개 쌓여 있을 때 (맨 위 = 3번째 높이) 집어 놓음
  blue all        파랑 줄0~3 을 차례로 집어 놓음 (놓을 때마다 엔터 대기 - 트레이 비우기)
  blue all 4      줄0~3 모두 4개 쌓인 높이에서
  pick red 1      집기만 (집어서 이동 높이까지 올라온 채 흡착 유지)
  put             pick 으로 쥐고 있는 보드를 트레이에 놓음
  home            HOME 으로 이동
  off             흡착 끄기 (보드 떨어짐)
  q               끝 (흡착 끄고 HOME 복귀)

동작은 메인 pick_and_place 와 같은 순서:
  이동 높이로 상승 -> 픽업 위 -> 하강 -> 흡착 ON -> 상승 -> (중간점) -> 트레이 위 -> 색별 놓기 R 회전
  -> 하강 -> 흡착 OFF -> 상승 -> 손목 0도
도착 실패하면 흡착 끄고 올라온 뒤 멈춤 (다시 입력하면 재시도).
안전: 반경 230~330mm 밖, 관절1 125° 초과 자리는 경고하고 y/n 확인.

!! 메인(main_board_robot_arm.py)과 DobotLab 등 같은 팔을 쓰는 프로그램은 끄고 실행할 것
"""

import math
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # 루트의 board_config.py

import board_drain_test as d          # 같은 폴더 - 이동·흡착 함수(move_to, travel, suck)와 팔 연결을 그대로 씀
from board_config import (
    BOARD_COLORS, NUM_ROWS, ROW_CAPACITY, HOME_XYZ, HOME_WRIST_DEG, PLACE_XYZ,
    PLACE_R_OFFSET_DEG_BY_COLOR, PRECISION_SETTLE_SEC, SUCTION_SETTLE_DELAY,
    pick_pose_for, travel_z_for, wrist_of, r_for, place_r_for, DOBOT_PORT,
)

COLOR_KEYS = {"r": "red", "g": "green", "b": "blue", "y": "yellow",
              "red": "red", "green": "green", "blue": "blue", "yellow": "yellow"}
REACH_MIN, REACH_MAX, ANGLE_MAX = 230.0, 330.0, 125.0


def check_spot(name, x, y):
    """위험한 자리면 경고 후 확인. 반환: 진행해도 되는지"""
    reach = math.hypot(x, y)
    ang = math.degrees(math.atan2(y, x))
    warns = []
    if reach < REACH_MIN:
        warns.append(f"반경 {reach:.0f}mm 가 {REACH_MIN:.0f} 안쪽 (들 때 알람 0x12 가능)")
    if reach > REACH_MAX:
        warns.append(f"반경 {reach:.0f}mm 가 {REACH_MAX:.0f} 바깥 (명령이 무시되기 쉬움)")
    if abs(ang) > ANGLE_MAX:
        warns.append(f"관절1 각도 {ang:.0f}° 가 ±{ANGLE_MAX:.0f}° 밖")
    if not warns:
        return True
    print(f"  ⚠ {name}: " + " / ".join(warns))
    return input("  그래도 진행할까요? (y/n): ").strip().lower() == "y"


class State:
    def __init__(self):
        self.xyz = HOME_XYZ
        self.wrist = HOME_WRIST_DEG
        self.holding = None      # 쥐고 있는 보드의 색 (pick 만 했을 때)


S = State()


def go_home():
    hx, hy, hz = HOME_XYZ
    # HOME 은 높은 자리라 그냥 올라간 뒤 이동
    d.move_to(S.xyz[0], S.xyz[1], max(S.xyz[2], hz), r_for(S.xyz[0], S.xyz[1], S.wrist), label="상승")
    d.travel((S.xyz[0], S.xyz[1], max(S.xyz[2], hz)), (hx, hy, hz), S.wrist, "HOME")
    S.xyz, S.wrist = HOME_XYZ, HOME_WRIST_DEG


def do_pick(color, row, n):
    """집어서 이동 높이까지 올라옴. 성공 여부 반환."""
    px, py, pz, pr = pick_pose_for(color, row, n)
    if not check_spot(f"픽업 자리({color} 줄{row})", px, py):
        return False
    wrist = wrist_of(px, py, pr)
    tz = travel_z_for(pz)
    cx, cy, cz = S.xyz
    print(f"  [{color}] 줄{row} {n}개 높이 자리 ({px:.1f},{py:.1f},{pz:.1f}) R={pr:.1f}", flush=True)

    d.move_to(cx, cy, max(cz, tz), r_for(cx, cy, S.wrist), label="상승")
    d.travel((cx, cy, max(cz, tz)), (px, py, tz), wrist, "픽업 위치 위")
    S.xyz, S.wrist = (px, py, tz), wrist
    if not d.move_to(px, py, pz, r_for(px, py, wrist), label="픽업 하강", settle_sec=PRECISION_SETTLE_SEC):
        d.move_to(px, py, tz, r_for(px, py, wrist), label="상승")
        print("  !! 픽업 하강 실패 - 멈춤 (다시 입력하면 재시도)", flush=True)
        return False
    d.suck(True)
    d.time.sleep(SUCTION_SETTLE_DELAY)
    d.move_to(px, py, tz, r_for(px, py, wrist), label="들어올리기")
    S.holding = color
    return True


def do_place(color):
    """쥐고 있는 보드를 트레이에 놓고 올라옴. 성공 여부 반환."""
    place_x, place_y, place_z = PLACE_XYZ
    if not check_spot("놓는 자리", place_x, place_y):
        return False
    cx, cy, cz = S.xyz
    tz = max(travel_z_for(0), cz)   # 이동 높이 (꽉 찼을 때 제일 높은 자리 기준 + 여유). 이미 더 높으면 그대로
    d.travel((cx, cy, tz), (place_x, place_y, tz), S.wrist, "트레이 위")
    pr = place_r_for(color)
    d.move_to(place_x, place_y, tz, pr, label=f"놓기 R 회전 ({PLACE_R_OFFSET_DEG_BY_COLOR[color]:+.0f}도, 절대 R {pr:.0f})")
    S.xyz = (place_x, place_y, tz)
    ok = d.move_to(place_x, place_y, place_z, pr, label="트레이 하강", settle_sec=PRECISION_SETTLE_SEC)
    d.suck(False)
    S.holding = None
    d.move_to(place_x, place_y, tz, pr, label="상승")
    d.move_to(place_x, place_y, tz, r_for(place_x, place_y, HOME_WRIST_DEG), label="손목 0도")
    S.wrist = HOME_WRIST_DEG
    if not ok:
        print("  !! 트레이 하강 실패 - 보드는 그 높이에서 떨어졌을 수 있음", flush=True)
    return ok


def parse_spot(tokens):
    """['red','0','3'] -> (color, row 또는 'all', n). 잘못되면 None"""
    if len(tokens) < 2:
        return None
    color = COLOR_KEYS.get(tokens[0])
    if color is None or color not in BOARD_COLORS:
        return None
    row = tokens[1]
    if row != "all":
        try:
            row = int(row)
        except ValueError:
            return None
        if not 0 <= row < NUM_ROWS:
            return None
    n = ROW_CAPACITY
    if len(tokens) >= 3:
        try:
            n = int(tokens[2])
        except ValueError:
            return None
        if not 1 <= n <= ROW_CAPACITY:
            return None
    return color, row, n


def run_one(color, row, n):
    if not do_pick(color, row, n):
        return False
    return do_place(color)


def main():
    print("보드 집고 놓기 테스트 - 팔이 실제로 움직입니다. 보드 main 은 꺼 두세요.")
    print("  예) red 0 / green 2 3 / blue all / pick red 1 → put / home / off / q", flush=True)
    print("연결:", d.loop.run_until_complete(d.safe_call(d.arm_api.ConnectDobot(portName=DOBOT_PORT))), flush=True)
    try:
        hx, hy, hz = HOME_XYZ
        d.move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="HOME")
        while True:
            line = input("\n명령> ").strip().lower()
            if not line:
                continue
            tok = line.split()
            cmd = tok[0]
            if cmd in ("q", "quit"):
                break
            if cmd == "home":
                go_home()
            elif cmd == "off":
                d.suck(False)
                S.holding = None
            elif cmd == "put":
                if not S.holding:
                    print("  쥐고 있는 보드가 없습니다 (pick 먼저).")
                else:
                    print("  결과:", "성공" if do_place(S.holding) else "실패", flush=True)
            elif cmd == "pick":
                spot = parse_spot(tok[1:])
                if not spot or spot[1] == "all":
                    print("  입력 예: pick red 1  /  pick green 2 3")
                elif S.holding:
                    print("  이미 쥐고 있음 - put 또는 off 먼저.")
                else:
                    do_pick(*spot) and print("  집음. 'put' 으로 놓거나 'off' 로 끄세요.")
            else:
                spot = parse_spot(tok)
                if not spot:
                    print("  입력이 잘못됨 (예: red 0 / green 2 3 / blue all / pick red 1 / put / home / off / q)")
                    continue
                if S.holding:
                    print("  보드를 쥐고 있음 - put 또는 off 먼저.")
                    continue
                color, row, n = spot
                rows = range(NUM_ROWS) if row == "all" else [row]
                label = f"{color} 줄 {'0~3' if row == 'all' else row} ({n}개 높이)"
                if input(f"  {label} 을(를) 집어서 트레이에 놓습니다. 트레이 자리가 비었고 주변이 비었습니까? (y/n): ").strip().lower() != "y":
                    continue
                for r in rows:
                    print(f"\n=== {color} 줄{r} ===", flush=True)
                    ok = run_one(color, r, n)
                    print("  결과:", "성공" if ok else "실패", flush=True)
                    if not ok:
                        break
                    if row == "all" and r != rows[-1]:
                        if input("  >>> 놓았음 - 트레이 비우고 엔터 (q=그만): ").strip().lower() == "q":
                            break
    except (KeyboardInterrupt, EOFError):
        print("\n중단")
    finally:
        d.suck(False)
        try:
            print("HOME 복귀", flush=True)
            go_home()
        except Exception as e:
            print("HOME 복귀 실패:", e)
        d.loop.run_until_complete(d.safe_call(d.arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        print("연결 해제", flush=True)


if __name__ == "__main__":
    main()
