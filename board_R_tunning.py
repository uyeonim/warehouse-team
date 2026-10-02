# -*- coding: utf-8 -*-
"""
트레이 놓기 확인/튜닝 - 보드를 실제로 집어서 트레이 위(안전 높이)까지 가져가 보고,
그 자리에서 R을 +/- 해보는 도구. 튜닝값은 전부 board_config.py에서 수정.

메인 코드는 트레이 위에 도착하면 R을 "기준 R(손목 0도) + 색별 회전값"으로 돌린 뒤 하강함
(board_config의 PLACE_R_OFFSET_DEG_BY_COLOR). 이 스크립트도 같은 R에서 시작하고, 여기서 맞춘
최종 R을 "그 색의 회전값"으로 환산해서 출력해줌.

흐름: HOME -> 픽업 위 -> 픽업 하강 -> 흡착 -> 들어올림 -> (중간점) -> 트레이 위(안전 높이)
      -> R 조정 루프 -> release(흡착 OFF) -> HOME
"""

import asyncio
import time

from DobotRPC import RPCClient, DobotlinkAdapter

from board_config import (
    DOBOT_PORT, NUM_ROWS, ROW_CAPACITY, BOARD_COLORS, PLACE_XYZ, PLACE_R_OFFSET_DEG_BY_COLOR,
    HOME_XYZ, HOME_WRIST_DEG,
    SAFE_LIFT_MM, MOVE_TIMEOUT_SEC, MOVE_POLL_INTERVAL_SEC, PRECISION_SETTLE_SEC,
    MOVE_STABLE_MM, MOVE_STABLE_DEG, MOVE_RESEND_AFTER_SEC, MOVE_MAX_RESEND,
    POSITION_TOLERANCE_MM, POSITION_TOLERANCE_DEG,
    SUCTION_SETTLE_DELAY, RELEASE_SETTLE_DELAY,
    pick_pose_for, wrist_of, r_for, place_r_for, base_angle, arc_midpoint, needs_midpoint, _wrap_deg,
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
    status = f"도착 {elapsed:.1f}초" if arrived else f"!! 도착 실패({elapsed:.1f}초)"
    if resends:
        status += f", 재전송 {resends}회"
    print(f"[{label}] 명령:{res} {status} 목표=({x:.1f},{y:.1f},{z:.1f},{r:.1f}) 실제:{actual}")
    return actual


def suck(on):
    res = loop.run_until_complete(safe_call(
        arm_api.SetEndEffectorSuctionCup(portName=DOBOT_PORT, enable=True, on=on, isQueued=False)
    ))
    print(f"흡착 {'ON' if on else 'OFF'} 결과:", res)


def travel(from_xyz, to_xyz, wrist, label, final_settle=0.0):
    """메인 코드의 travel()과 같은 규칙 - 각도 차 크면 극좌표 중간점 경유."""
    if needs_midpoint(from_xyz[:2], to_xyz[:2]):
        mx, my = arc_midpoint(from_xyz[:2], to_xyz[:2])
        mz = max(from_xyz[2], to_xyz[2])
        move_to(mx, my, mz, r_for(mx, my, wrist), label=f"{label} - 중간점 경유")
    tx, ty, tz = to_xyz
    move_to(tx, ty, tz, r_for(tx, ty, wrist), label=label, settle_sec=final_settle)


def parse_row_count(raw):
    parts = raw.strip().split("/")
    if len(parts) != 2:
        return None
    try:
        row, cnt = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= row < NUM_ROWS) or not (1 <= cnt <= ROW_CAPACITY):
        return None
    return row, cnt


def disconnect():
    loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
    print("연결 해제 완료")


def main():
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT)))
    print("연결 결과:", connect_res)
    print("현재 위치:", get_pose())

    hx, hy, hz = HOME_XYZ
    move_to(hx, hy, hz, r_for(hx, hy, HOME_WRIST_DEG), label="초기위치로 이동")

    color = input("\n픽업할 색깔 (red/yellow/blue/green): ").strip().lower()
    if color not in BOARD_COLORS:
        print(f"알 수 없는 색상 - {BOARD_COLORS} 중 하나")
        return disconnect()

    parsed = parse_row_count(input(f"줄/개수 (예: 0/4, 줄 0~{NUM_ROWS - 1}, 개수 1~{ROW_CAPACITY}): "))
    if parsed is None:
        print("형식이 안 맞음")
        return disconnect()
    row, cnt = parsed

    px, py, pz, pr = pick_pose_for(color, row, cnt)
    wrist = wrist_of(px, py, pr)
    place_x, place_y, place_z = PLACE_XYZ
    travel_z = max(pz, place_z) + SAFE_LIFT_MM

    print(f"\n[{color}] 줄{row}/{cnt}개 -> 픽업 ({px:.1f},{py:.1f},{pz:.1f}) R={pr:.1f} 손목={wrist:.1f}")
    if input("이 위치로 이동할까요? (y/n): ").strip().lower() != "y":
        print("취소")
        return disconnect()

    travel((hx, hy, travel_z), (px, py, travel_z), wrist, "픽업 위치 위로 이동")
    move_to(px, py, pz, r_for(px, py, wrist), label="픽업 위치로 하강", settle_sec=PRECISION_SETTLE_SEC)
    suck(True)
    time.sleep(SUCTION_SETTLE_DELAY)
    move_to(px, py, travel_z, r_for(px, py, wrist), label="안전 높이로 들어올리기")

    print(f"\n트레이 위({place_x:.1f},{place_y:.1f}) 안전 높이({travel_z:.1f})까지 이동 - 하강은 안 함")
    if input("트레이 위치로 이동할까요? (y/n): ").strip().lower() != "y":
        print("취소 - 흡착 끄고 종료 (지금 높이에서 떨어짐)")
        suck(False)
        return disconnect()
    travel((px, py, travel_z), (place_x, place_y, travel_z), wrist, "트레이 위로 이동")

    base_r = base_angle(place_x, place_y)   # 기준 R (손목 0도)
    cur_offset = PLACE_R_OFFSET_DEG_BY_COLOR[color]
    current_r = place_r_for(color)
    move_to(place_x, place_y, travel_z, current_r,
            label=f"트레이 위에서 R 회전 ({color} 현재 회전값 {cur_offset:+.1f}도)")
    step = 1.0
    print("\nR만 바꿔가며 방향 확인 (X,Y,Z 고정).")
    print("  +        -> 스텝만큼 증가 / -        -> 스텝만큼 감소")
    print("  +5 / -5  -> 그만큼 1회 증감 / step 0.5 -> 기본 스텝 변경")
    print("  =-120 / =35 -> 그 R로 절대 이동 (음수 절대값은 반드시 = 붙일 것)")
    print("  숫자(부호 없음) -> 그 R로 절대 이동 / release -> 흡착 끄고 종료 (이 높이에서 떨어짐)")
    while True:
        cmd = input(f"\n현재 R={current_r:.4f} (스텝={step}) / 명령: ").strip().lower()

        if cmd == "release":
            if input(f"z={travel_z:.1f}에서 흡착을 끌까요? 보드가 떨어집니다. (y/n): ").strip().lower() == "y":
                break
            continue

        if cmd.startswith("step"):
            parts = cmd.split()
            try:
                step = float(parts[1])
                print(f"스텝을 {step}도로 변경")
            except (IndexError, ValueError):
                print("사용법: step 0.5")
            continue

        try:
            if cmd.startswith("="):
                new_r = float(cmd[1:])
            elif cmd == "+":
                new_r = current_r + step
            elif cmd == "-":
                new_r = current_r - step
            elif cmd.startswith(("+", "-")):
                new_r = current_r + float(cmd)
            else:
                new_r = float(cmd)
        except ValueError:
            print("=숫자, 숫자, +, -, +숫자, -숫자, 'step 숫자', 'release' 중 하나")
            continue

        move_to(place_x, place_y, travel_z, new_r, label="R 조정")
        current_r = new_r

    suck(False)
    time.sleep(RELEASE_SETTLE_DELAY)

    new_offset = current_r - base_r
    print(f"\n최종 R={current_r:.4f} (기준 R {base_r:.4f}에서 {new_offset:+.2f}도)")
    print(f"-> 메인 코드에 반영하려면 board_config.py의 PLACE_R_OFFSET_DEG_BY_COLOR['{color}']를 "
          f"{new_offset:.2f}로 바꾸면 됨 (현재 {cur_offset:.2f})")

    move_to(place_x, place_y, travel_z, r_for(place_x, place_y, HOME_WRIST_DEG), label="R 원위치 (손목 0도)")

    travel((place_x, place_y, travel_z), HOME_XYZ, HOME_WRIST_DEG, "초기위치로 복귀")
    disconnect()


if __name__ == "__main__":
    main()
