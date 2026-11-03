# -*- coding: utf-8 -*-
"""
현재 위치 찍기 - 보드 팔을 손으로 옮겨 놓고 Enter 치면 지금 좌표를 읽어 board_config.py 형식으로 출력.
팔은 전혀 움직이지 않음 (연결 + GetPose만). 보드 main을 끄고 실행할 것 (같은 포트를 씀).

입력:
  home          : 대기 위치 (HOME_XYZ)
  place         : 트레이에 "닿는" 높이에서 찍기 -> PLACE_XYZ는 z + PLACE_DROP_MM (공중에서 흡착 OFF)
  red 3 / green 0 ... : 픽업 자리 (색, 줄 0=제일 가까움 ~ 3=제일 멈). 그 자리에 쌓인 개수를 물어보고
                  floor_z = 실측 z - 개수 × BOARD_THICKNESS_MM 로 계산
  (빈 Enter)    : 그냥 지금 위치만 표시
  ... # 메모     : 뒤에 # 메모를 붙이면 같이 기록 (예: red 3 # 수납함 위, 컵 살짝 기울어짐). 안 붙여도 됨
  # 메모         : 메모만 기록 (좌표 없이)
  q             : 종료 - 찍은 값 전부 다시 출력
찍은 값은 board/logs/read_pose_YYYYMMDD.txt 에도 쌓임 (board_config.py에 반영은 손으로)
"""

import asyncio
import math
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import os as _os, sys as _sys
_BOARD = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _BOARD)  # 루트의 board_config.py
from board_config import DOBOT_PORT, DOBOT_SERIAL, BOARD_COLORS, NUM_ROWS, ROW_CAPACITY, BOARD_THICKNESS_MM

PLACE_DROP_MM = 10.0   # board_config PLACE_XYZ 주석: 트레이에 닿는 높이 + 10mm
# 손으로는 갈 수 있어도 명령으로는 못 가는 자리 (keycap_tray/참고사항.md 1장 실측):
#   관절1 135°에서 한계 알람 -> 120° 여유. 반경 330mm 넘으면 명령 무시·재전송이 잦음
#   2026-11-03 PLACE를 관절1 -136.6°, 반경 362mm에 찍었다가 HOME 명령이 전부 무시됨
J1_LIMIT_DEG = 120.0
REACH_MAX_MM = 330.0


def reach_warning(x, y):
    """명령으로 못 갈 수 있는 자리면 이유, 괜찮으면 빈 문자열 (관절1 = 베이스 각도 atan2(y, x))"""
    j1, radius = math.degrees(math.atan2(y, x)), math.hypot(x, y)
    bad = []
    if abs(j1) > J1_LIMIT_DEG:
        bad.append(f"관절1 {j1:.1f}° (±{J1_LIMIT_DEG:g}° 넘음)")
    if radius > REACH_MAX_MM:
        bad.append(f"반경 {radius:.0f}mm ({REACH_MAX_MM:g}mm 넘음)")
    return ", ".join(bad)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
arm_api = dobotlink.MagicianLite


async def safe_call(coro, timeout_sec=5):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def get_pose():
    pose = loop.run_until_complete(safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False)))
    if not isinstance(pose, dict):
        print("위치 읽기 실패:", pose)
        return None
    return pose


def record(lines, log_path, text):
    print(text)
    lines.append(text)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')}  {text}\n")


def main():
    if DOBOT_PORT is None:
        print(f"보드 팔(USB 시리얼 {DOBOT_SERIAL})이 안 보임 - USB/전원 확인")
        return
    _os.makedirs(_os.path.join(_BOARD, "logs"), exist_ok=True)
    log_path = _os.path.join(_BOARD, "logs", f"read_pose_{time.strftime('%Y%m%d')}.txt")
    print("연결:", DOBOT_PORT, loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))))
    print("팔은 안 움직임. 잠금 버튼 누른 채 손으로 옮겨 놓고 입력")
    print(f"  home / place / <색> <줄>  (색: {', '.join(BOARD_COLORS)}, 줄 0~{NUM_ROWS - 1})  / 빈 Enter=위치만 / q=종료")
    print("  뒤에 '# 메모' 붙이면 같이 기록 (예: red 3 # 수납함 위). '# 메모'만 쳐도 기록")
    lines = []
    try:
        while True:
            raw, _, memo = input("\n> ").partition("#")
            memo = memo.strip()
            cmd = raw.strip().lower().split()
            if cmd == ["q"]:
                break
            if not cmd and memo:
                record(lines, log_path, f"# {memo}")
                continue
            pose = get_pose()
            if pose is None:
                continue
            x, y, z, r = pose["x"], pose["y"], pose["z"], pose["r"]
            warn = reach_warning(x, y)
            if warn:
                print(f"!! 명령으로 못 갈 수 있는 자리: {warn} - 자리를 팔 쪽/앞쪽으로 옮길 것")
                memo = f"{memo} / !! {warn}" if memo else f"!! {warn}"
            note = f", {memo}" if memo else ""
            if not cmd:
                print(f"지금: x={x:.1f} y={y:.1f} z={z:.1f} r={r:.1f}")
                if memo:
                    record(lines, log_path, f"# 위치 ({x:.4f}, {y:.4f}, {z:.4f}, {r:.4f}) - {memo}")
            elif cmd == ["home"]:
                record(lines, log_path, f"HOME_XYZ = ({x:.4f}, {y:.4f}, {z:.4f})" + (f"  # {memo}" if memo else ""))
            elif cmd == ["place"]:
                record(lines, log_path, f"PLACE_XYZ = ({x:.4f}, {y:.4f}, {z + PLACE_DROP_MM:.4f})"
                                        f"  # 닿는 높이 {z:.4f} + {PLACE_DROP_MM:g}mm{note}")
            elif len(cmd) == 2 and cmd[0] in BOARD_COLORS and cmd[1].isdigit() and int(cmd[1]) < NUM_ROWS:
                cnt = input(f"  그 자리에 쌓인 보드 개수 (0~{ROW_CAPACITY}, 맨 위 보드 윗면에서 찍었으면 그 개수): ").partition("#")[0].strip()
                if not cnt.isdigit() or int(cnt) > ROW_CAPACITY:
                    print("  개수가 이상함 - 다시")
                    continue
                floor_z = z - int(cnt) * BOARD_THICKNESS_MM
                record(lines, log_path, f"{cmd[0]} row{cmd[1]}: ({x:.4f}, {y:.4f}, {floor_z:.4f}, {r:.4f}),"
                                        f"  # 실측 {time.strftime('%Y-%m-%d')} ({cnt}개, z={z:.1f}){note}")
            else:
                print("모르는 입력 - home / place / red 3 / q")
    finally:
        if lines:
            print("\n=== 찍은 값 (board_config.py에 반영) ===")
            print("\n".join(lines))
            print(f"(저장: {log_path})")
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        print("연결 해제 완료")


if __name__ == "__main__":
    main()
