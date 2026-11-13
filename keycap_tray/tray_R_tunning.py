# -*- coding: utf-8 -*-
"""
트레이 놓기 R 튜닝 - 트레이를 실제로 집어서 벨트 놓는 자리 위(안전 높이)까지 가져가고,
그 자리에서 R(손목 회전)만 돌려 보면서 트레이가 벨트에 반듯하게 놓이는 방향을 맞추는 도구. (보드 팔의 board_R_tunning.py 와 같은 방식)

사용 (게이트웨이·보드 main 을 끄고 실행 - 같은 팔을 씀. 팔이 실제로 움직임):
  python tray_R_tunning.py            남은 개수로 정한 "다음에 집을 자리"에서 집음
  python tray_R_tunning.py 3 5        더미3 5층에서 집음 (더미 번호, 층)
  ※ 남은 개수는 건드리지 않음 (빼지 않음). 시험하려면 그 자리에 트레이가 있어야 함

흐름: 그리퍼 열기 -> 집는 자리 -> 집기 -> 놓는 자리 위(안전 높이, 놓는 z + 40mm)로 이동
      -> R 조정 루프 -> (down 으로) 내려가서 놓기 -> 저장할지 묻기 -> 집는 자리 위로 비킴

R 조정 명령 (X·Y·Z 는 그대로, R 만 바뀜):
  +  /  -            스텝만큼 증가 / 감소 (처음 스텝 1도)
  +5 / -5            그만큼 한 번 증감
  step 0.5           스텝 바꾸기
  =108.8 / =-30      그 R 로 이동 (음수는 반드시 = 붙임)
  108.8              (부호 없는 숫자) 그 R 로 이동
  down               지금 R 로 놓는 자리까지 내려가서 놓고 위로 올라옴 (y/n 확인)
  release            이 높이에서 그냥 놓음 (트레이가 떨어짐 - 급할 때만)
  q                  끝 (트레이를 들고 있으면 down 이나 release 를 먼저 하라고 안내)
끝나면 최종 R 을 보여 주고 pose_tray.json 의 place_r[더미] 에 저장할지 물음 (저장 전 .bak-날짜 백업).
★ 더미 단위: "3 5" 로 맞추면 더미3 의 모든 층(1~5층)에 같은 R 이 적용됨. 더미1·2·3 은 각각 따로 맞춰야 함
  (더미마다 집을 때의 방향이 달라서 벨트에 놓을 때 R 도 달라짐). 안 맞춘 더미는 place_slots[1] 의 R 을 씀.
R = 베이스 각도 + 손목. 손목 0도 = 놓는 곳 방향 그대로 (베이스 각도와 같은 R).
"""

import asyncio
import json
import math
import os
import shutil
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import dobot_control as dc
from worker_lock import WorkerLock

HERE = os.path.dirname(os.path.abspath(__file__))
POSE_FILE = os.path.join(HERE, "pose_tray.json")
HOVER_MM = 40.0   # 놓는 자리 위에서 R 을 돌릴 높이 = 놓는 z + 이만큼 (트레이 아랫면이 벨트에 안 닿게)
WRIST_WARN = 130.0  # 손목이 이만큼 이상 돌면 관절4 한계가 가까우니 경고


def base_angle(x, y):
    return math.degrees(math.atan2(y, x))


def parse_r(cmd, current, step):
    """명령 문자열 -> 새 R (못 읽으면 None)"""
    try:
        if cmd.startswith("="):
            return float(cmd[1:])
        if cmd == "+":
            return current + step
        if cmd == "-":
            return current - step
        if cmd.startswith(("+", "-")):
            return current + float(cmd)
        return float(cmd)
    except ValueError:
        return None


def save_place_r(new_r, stack_no, path=POSE_FILE):
    """pose_tray.json 의 place_r[더미] 에 저장 (그 더미의 모든 층에 적용). 반환: (이전 값, 새 값, 백업 파일)"""
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    base = saved["place_slots"]["1"][3]
    place_r = {str(k): v for k, v in (saved.get("place_r") or {}).items()}
    old = place_r.get(str(stack_no), base)
    place_r[str(stack_no)] = round(new_r, 1)
    backup = path + time.strftime(".bak-%Y%m%d-%H%M%S")
    shutil.copy2(path, backup)
    saved["place_r"] = place_r
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=2, ensure_ascii=False)
    return old, place_r[str(stack_no)], os.path.basename(backup)


async def tune(arm_api, port, stack_no, floor, ask=input, pose_path=POSE_FILE):
    """반환: 최종 R (끝까지 못 가면 None)"""
    stack = dc.tray_stock.STACKS[stack_no - 1]
    pick = stack[dc.tray_stock.FLOORS - floor]
    px, py, pz, pr = dc.TRAY["place_slots"][1]
    hover_z = pz + HOVER_MM
    base = base_angle(px, py)
    place_r = dc.tray_stock.place_r(stack_no, pr)   # 이 더미에 저장된 놓는 R (없으면 기본)
    holding = False

    async def move(pose, mode=dc.PTP_MODE_JUMP, label=""):
        _, ok, now = await dc.move_to(arm_api, port, pose, mode=mode, fallback=False)
        print(f"[{label}] 목표=({pose[0]:.1f},{pose[1]:.1f},{pose[2]:.1f},{pose[3]:.1f}) 도착:{ok}  현재:{now}")
        return ok

    async def cleanup(grip_off=True):
        if grip_off:
            await dc.gripper(arm_api, port, on=False)
        await dc.safe_call(arm_api.SetEndEffectorGripper(portName=port, enable=False, on=False, isQueued=False))
        if dc.TRAY.get("travel_z"):
            await dc.set_jump_height(arm_api, port, dc.JUMP_HEIGHT)
        await dc.clear_alarms_now(arm_api, port, "튜닝 끝 정리")

    print(f"\n더미{stack_no} {floor}층 {pick} 에서 트레이를 집어 벨트 놓는 자리 ({px:.1f},{py:.1f},{pz:.1f}) 위로 가져갑니다.")
    print(f"  놓는 곳 R 지금 {place_r:.1f} (베이스 각도 {base:.1f} + 손목 {place_r - base:+.1f}),"
          f" R 돌릴 높이 z={hover_z:.1f}")
    if ask("진행할까요? 벨트의 놓는 자리가 비어 있고 팔 주변에 손이 없어야 합니다. (y/n): ").strip().lower() != "y":
        print("취소")
        return None

    print(f"그리퍼 열기: {await dc.gripper(arm_api, port, on=False)}")
    await dc.lower_if_too_high(arm_api, port)
    await dc.use_travel_height(arm_api, port, dc.TRAY, pick)
    if not await move(pick, label="집는 자리"):
        print("!! 집는 자리에 못 갔음 - 중단 (팔을 손으로 비키고 다시 시도)")
        await cleanup()
        return None
    print(f"집기: {await dc.gripper(arm_api, port, on=True)}")
    holding = True

    await dc.use_travel_height(arm_api, port, dc.TRAY, (px, py, hover_z, place_r), min_lift=30.0)
    if not await move((px, py, hover_z, place_r), label="놓는 자리 위로"):
        print("!! 놓는 자리 위에 못 갔음 - 중단. 트레이를 쥐고 있습니다. 손으로 받치고 'release' 하거나 팔을 비키세요")
        await cleanup(grip_off=False)
        return None

    current_r, step, placed = place_r, 1.0, False
    print("\nR만 바꿔가며 방향 확인 (X, Y, Z 고정).")
    print("  +/-  +5/-5  step 0.5  =108.8  108.8  |  down(내려가서 놓기)  release(이 높이에서 놓기)  q(끝)")
    while True:
        cmd = ask(f"\n현재 R={current_r:.2f} (손목 {current_r - base:+.1f}, 스텝={step}) / 명령: ").strip().lower()
        if cmd == "q":
            if holding:
                print("트레이를 들고 있습니다. 'down'(내려가서 놓기) 또는 'release' 를 먼저 하세요.")
                continue
            break
        if cmd == "release":
            if ask(f"z={hover_z:.1f}에서 트레이를 놓을까요? 떨어집니다. (y/n): ").strip().lower() == "y":
                print(f"그리퍼 열기: {await dc.gripper(arm_api, port, on=False)}")
                holding, placed = False, True
            continue
        if cmd == "down":
            if ask(f"R={current_r:.2f} 로 z={pz:.1f}까지 내려가 놓을까요? (y/n): ").strip().lower() != "y":
                continue
            if await move((px, py, pz, current_r), mode=dc.PTP_MODE_MOVL, label="놓는 자리로 하강"):
                print(f"그리퍼 열기: {await dc.gripper(arm_api, port, on=False)}")
                holding, placed = False, True
                await asyncio.sleep(dc.RELEASE_SETTLE_DELAY)
                await move((px, py, hover_z, current_r), mode=dc.PTP_MODE_MOVL, label="위로 올라옴")
            else:
                print("!! 못 내려갔음 - 트레이를 쥔 채로 둡니다")
            continue
        if cmd.startswith("step"):
            try:
                step = float(cmd.split()[1])
                print(f"스텝을 {step}도로 변경")
            except (IndexError, ValueError):
                print("사용법: step 0.5")
            continue
        new_r = parse_r(cmd, current_r, step)
        if new_r is None:
            print("명령: +  -  +5  -5  step 0.5  =108.8  108.8  down  release  q 중 하나")
            continue
        if abs(new_r - base) > WRIST_WARN:
            print(f"!! 손목이 {new_r - base:+.1f}도로 관절4 한계(약 ±135도)에 가깝습니다 - 알람이 날 수 있음")
        if holding or not placed:
            if await move((px, py, hover_z, new_r), mode=dc.PTP_MODE_MOVJ, label="R 조정"):
                current_r = new_r
        else:
            current_r = new_r   # 이미 놓았으면 팔은 안 움직이고 값만 바꿔 봄

    print(f"\n최종 R={current_r:.2f} (놓는 곳 베이스 각도 {base:.1f}에서 손목 {current_r - base:+.1f}도)")
    if abs(current_r - place_r) < 0.05:
        print("지금 저장된 값과 같아서 저장할 게 없습니다.")
    elif ask(f"더미{stack_no} 의 놓는 R 을 {place_r:.1f} -> {current_r:.1f} 로 저장할까요? "
             f"(그 더미의 모든 층에 적용됩니다) (y/n): ").strip().lower() == "y":
        old, new, backup = save_place_r(current_r, stack_no, pose_path)
        print(f"저장함: place_r[더미{stack_no}] {old} -> {new}  (백업: {backup})")
        print("  다른 더미(1·2·3)도 각각 같은 방법으로 맞추세요. 안 맞춘 더미는 기본 R 을 씁니다.")
        print("  게이트웨이를 켜 둔 상태였다면 껐다가 다시 켜야 새 값이 적용됩니다.")
    else:
        print("저장하지 않았습니다.")

    await dc.use_travel_height(arm_api, port, dc.TRAY, (pick[0], pick[1], pick[2] + dc.TRAY.get("away_lift_z", dc.LIFT_Z_OFFSET), pick[3]))
    await move((pick[0], pick[1], pick[2] + dc.TRAY.get("away_lift_z", dc.LIFT_Z_OFFSET), pick[3]), label="집는 자리 위로 비킴")
    await cleanup()
    return current_r


def main():
    if not dc.tray_stock.enabled() or not (dc.TRAY["place_slots"][1]):
        raise SystemExit("트레이 더미·놓는 곳 좌표가 없습니다 (pose_tray.json). 먼저 read_pose.py tray")
    port = dc.TRAY["port"]
    if not port:
        raise SystemExit("트레이 팔 USB 가 안 보입니다 (전원·케이블 확인)")
    if len(sys.argv) >= 3:
        stack_no, floor = int(sys.argv[1]), int(sys.argv[2])
    else:
        count = dc.tray_stock.cached()
        if count <= 0:
            raise SystemExit("남은 개수가 0 입니다. 더미 번호와 층을 직접 주세요: python tray_R_tunning.py 3 5")
        stack_no, floor, _ = dc.tray_stock.pick_pose(count)
        print(f"남은 개수 {count} -> 다음 집을 자리: 더미{stack_no} {floor}층")
    n = len(dc.tray_stock.STACKS)
    if not (1 <= stack_no <= n and 1 <= floor <= dc.tray_stock.FLOORS):
        raise SystemExit(f"더미는 1~{n}, 층은 1~{dc.tray_stock.FLOORS}")

    with WorkerLock():   # 게이트웨이가 켜져 있으면 여기서 멈춤
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
        arm_api = getattr(dobotlink, dc.NAMESPACE_ARM)
        print(f"tray ({port}) 연결: {loop.run_until_complete(dc.safe_call(arm_api.ConnectDobot(portName=port)))}")
        try:
            loop.run_until_complete(tune(arm_api, port, stack_no, floor))
        except KeyboardInterrupt:
            print("\n중단 - 팔·그리퍼 상태를 확인하세요 (트레이를 쥐고 있을 수 있음)")
        finally:
            loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        if "게이트웨이" not in str(e):
            raise
        print("\n!! 창고 프로그램(게이트웨이)이 켜져 있어서 로봇을 쓸 수 없습니다 (같은 팔·벨트를 같이 못 씀).")
        print("   바탕화면 '창고 끄기'를 누른 뒤 다시 실행하세요.")
        raise SystemExit(1)
