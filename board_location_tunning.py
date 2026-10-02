# -*- coding: utf-8 -*-
"""
픽업 위치 튜닝 - 색/줄/개수를 직접 입력해서 그 픽업 위치로 이동해보는 도구.
좌표 등 튜닝값은 전부 board_config.py에서 수정 (여기엔 값 없음).

이동 방식은 메인 코드와 동일:
  현재 위치에서 안전 높이로 올림 -> (각도 차 크면 극좌표 중간점) -> 목표 위 -> 하강
R은 픽업 R로 정해지는 손목 각도를 유지하며 자동 계산.

입력:
  색깔  : red / yellow / blue / green
  줄/개수: '0/4' 형식 (줄 0=제일 가까움 ~ 3=제일 멈, 개수=그 줄에 쌓인 개수 1~5)
  home  : 초기위치로 복귀
  q     : 종료
"""

import asyncio
import time

from DobotRPC import RPCClient, DobotlinkAdapter

from board_config import (
    DOBOT_PORT, NUM_ROWS, ROW_CAPACITY, BOARD_COLORS, HOME_XYZ, HOME_WRIST_DEG, SAFE_LIFT_MM,
    MOVE_TIMEOUT_SEC, MOVE_POLL_INTERVAL_SEC, PRECISION_SETTLE_SEC,
    MOVE_STABLE_MM, MOVE_STABLE_DEG, MOVE_RESEND_AFTER_SEC, MOVE_MAX_RESEND,
    POSITION_TOLERANCE_MM, POSITION_TOLERANCE_DEG,
    pick_pose_for, wrist_of, r_for, arc_midpoint, needs_midpoint, _wrap_deg,
)

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
    return loop.run_until_complete(safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False)))


def _matches(actual, target):
    if not isinstance(actual, dict):
        return False
    tx, ty, tz, tr = target
    try:
        return (abs(actual["x"] - tx) <= POSITION_TOLERANCE_MM
                and abs(actual["y"] - ty) <= POSITION_TOLERANCE_MM
                and abs(actual["z"] - tz) <= POSITION_TOLERANCE_MM
                and abs(_wrap_deg(actual["r"] - tr)) <= POSITION_TOLERANCE_DEG)
    except (KeyError, TypeError):
        return False


def _is_still(prev, cur):
    """직전 조회 대비 거의 안 움직였으면 True (= 팔이 멈춘 상태)."""
    try:
        return (abs(cur["x"] - prev["x"]) <= MOVE_STABLE_MM
                and abs(cur["y"] - prev["y"]) <= MOVE_STABLE_MM
                and abs(cur["z"] - prev["z"]) <= MOVE_STABLE_MM
                and abs(_wrap_deg(cur["r"] - prev["r"])) <= MOVE_STABLE_DEG)
    except (KeyError, TypeError):
        return False


def move_to(x, y, z, r, label="", settle_sec=0.0):
    """메인 코드와 같은 이동 방식 - 도착 + 멈춤까지 GetPose로 확인, 목표 못 간 채 멈춰 있으면
    명령 재전송 (최대 MOVE_MAX_RESEND번), 최대 MOVE_TIMEOUT_SEC."""
    def send():
        return loop.run_until_complete(safe_call(
            arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0, x=x, y=y, z=z, r=r, isQueued=False)
        ))

    started = time.time()
    res = send()
    actual, arrived, resends = None, False, 0
    prev = None
    still_since = time.time()
    while time.time() - started < MOVE_TIMEOUT_SEC:
        time.sleep(MOVE_POLL_INTERVAL_SEC)
        actual = get_pose()
        if not isinstance(actual, dict):
            continue
        if prev is None or not _is_still(prev, actual):
            still_since = time.time()   # 아직 움직이는 중
        elif _matches(actual, (x, y, z, r)):
            arrived = True
            break
        elif time.time() - still_since >= MOVE_RESEND_AFTER_SEC:
            if resends >= MOVE_MAX_RESEND:
                break
            resends += 1
            res = send()
            print(f"[{label}] 목표 못 간 채 멈춤 -> 명령 재전송 {resends}/{MOVE_MAX_RESEND} ({res})")
            still_since = time.time()
        prev = actual
    if arrived and settle_sec > 0:
        time.sleep(settle_sec)
    elapsed = time.time() - started
    status = f"도착 {elapsed:.1f}초" if arrived else f"⚠️ 도착 실패({elapsed:.1f}초)"
    if resends:
        status += f", 재전송 {resends}회"
    print(f"[{label}] 명령:{res} {status} 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}) 실제:{actual}")
    return actual


def travel(from_xyz, to_xyz, wrist, label, final_settle=0.0):
    """메인 코드의 travel()과 같은 규칙 - 각도 차 크면 극좌표 중간점 경유."""
    if needs_midpoint(from_xyz[:2], to_xyz[:2]):
        mx, my = arc_midpoint(from_xyz[:2], to_xyz[:2])
        mz = max(from_xyz[2], to_xyz[2])
        move_to(mx, my, mz, r_for(mx, my, wrist), label=f"{label} - 중간점 경유")
    tx, ty, tz = to_xyz
    return move_to(tx, ty, tz, r_for(tx, ty, wrist), label=label, settle_sec=final_settle)


def parse_row_count(raw):
    """'0/4' -> (0, 4). 형식/범위 안 맞으면 None."""
    parts = raw.strip().split("/")
    if len(parts) != 2:
        return None
    try:
        row, cnt = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= row < NUM_ROWS):
        print(f"줄 번호는 0~{NUM_ROWS - 1}")
        return None
    if not (1 <= cnt <= ROW_CAPACITY):
        print(f"개수는 1~{ROW_CAPACITY}")
        return None
    return row, cnt


def main():
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT)))
    print("연결 결과:", connect_res)
    print("현재 위치:", get_pose())

    hx, hy, hz = HOME_XYZ
    move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="초기위치로 이동")
    cur_xyz = HOME_XYZ
    cur_wrist = HOME_WRIST_DEG

    print("\n색깔 입력 (red/yellow/blue/green), 'home' 초기위치 복귀, 'q' 종료")
    print(f"줄/개수는 '0/4' 형식 - 줄 0(제일 가까움)~{NUM_ROWS - 1}(제일 멈), 개수 1~{ROW_CAPACITY}")
    while True:
        cmd = input("\n색깔: ").strip().lower()
        if cmd == "q":
            break

        if cmd == "home":
            safe_z = max(cur_xyz[2], hz) + SAFE_LIFT_MM
            move_to(cur_xyz[0], cur_xyz[1], safe_z, r_for(cur_xyz[0], cur_xyz[1], cur_wrist), label="들어올리기")
            travel((cur_xyz[0], cur_xyz[1], safe_z), HOME_XYZ, cur_wrist, "초기위치로 복귀")
            cur_xyz = HOME_XYZ
            continue

        if cmd not in BOARD_COLORS:
            print(f"알 수 없는 색상 - {BOARD_COLORS} 중 하나")
            continue

        parsed = parse_row_count(input("줄/개수 (예: 0/4): "))
        if parsed is None:
            print("형식이 안 맞음 - 예: 0/4")
            continue
        row, cnt = parsed

        px, py, pz, pr = pick_pose_for(cmd, row, cnt)
        wrist = wrist_of(px, py, pr)
        print(f"[{cmd}] 줄{row}/{cnt}개 -> 픽업 위치 ({px:.1f},{py:.1f},{pz:.1f}) R={pr:.1f} 손목={wrist:.1f}")
        if input("이 위치로 이동할까요? (y/n): ").strip().lower() != "y":
            print("취소")
            continue

        safe_z = max(cur_xyz[2], pz) + SAFE_LIFT_MM
        move_to(cur_xyz[0], cur_xyz[1], safe_z, r_for(cur_xyz[0], cur_xyz[1], wrist), label="들어올리기")
        travel((cur_xyz[0], cur_xyz[1], safe_z), (px, py, safe_z), wrist, "픽업 위치 위로 이동")
        target = (px, py, pz, r_for(px, py, wrist))
        actual = move_to(*target, label="픽업 위치로 하강", settle_sec=PRECISION_SETTLE_SEC)
        print("도착 확인:", "OK" if _matches(actual, target) else "실패 (목표와 다름)")

        cur_xyz = (px, py, pz)
        cur_wrist = wrist

    print("\n종료 중...")
    loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
    print("연결 해제 완료")


if __name__ == "__main__":
    main()
