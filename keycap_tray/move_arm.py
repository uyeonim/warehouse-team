# -*- coding: utf-8 -*-
"""
로봇팔 좌표 보기 + 움직이기 (대화형) - 지금 좌표를 확인하고, 숫자로 이동하거나 조금씩 움직여 봄 (조깅)

사용 (게이트웨이·보드 main 을 끄고 실행 - 같은 팔을 씀. 팔이 실제로 움직임):
  python move_arm.py            트레이 팔
  python move_arm.py set1       키캡 팔 세트1 (set2 도 가능)

명령 (소문자, Enter 만 치면 지금 좌표 다시 보기):
  pose                 지금 좌표 + 반경·각도·관절 + 알람
  goto x y z r         그 좌표로 이동 (올라갔다 옮겨서 내려감 = 평소 동작과 같은 JUMP, y/n 확인)
  dx 10 / dy -5        그 축으로 조금 직선 이동 (mm)
  dz 20 / dz -10       위(+)·아래(-)로 직선 이동 (mm)
  dr 5                 R(손목 회전)만 돌림 (도)
  out 5 / out -5       팔 베이스에서 바깥(+)·안쪽(-)으로 직선 이동 (mm, 높이·방향 그대로)
  slot 3 5             (트레이 팔) 더미3 5층 손잡이 바로 위 (손잡이 z + 20) 로 이동
  place                (트레이 팔) 벨트 놓는 자리 바로 위 (놓는 z + 40) 로 이동
  cycle 3 5            (트레이 팔) 더미3 5층에서 트레이를 실제로 집어 벨트에 놓고 빠짐까지 한 번에 (평소 주문 동작과 같음, 개수는 안 뺌)
  pick 3 5             (트레이 팔) 더미3 5층에서 집기만 (위로 이동 -> 내려가 쥐기 -> 올라옴). 쥔 채로 둠
  put                  (트레이 팔) 쥐고 있는 트레이를 벨트 놓는 자리에 놓고 위로 올라옴
  open / close         그리퍼 열기 / 닫기(쥐기)
  belt [초] [속도]     (키캡 팔 set1/set2) 그 세트의 벨트를 실제 운전과 같은 방향·속도로 돌리고 멈춤. 초 생략 = 실제 시간(CONVEYOR_RUN_TIME), 속도 생략 = 실제 속도
  back [초] [속도]     같은데 반대 방향 (키캡을 되돌릴 때)
  beltstop             벨트 바로 정지
  clear                알람 해제 (팔은 안 움직임)
  home                 원점 복귀 (팔이 홈 위치로 움직임, y/n 확인)
  q                    끝
안전: 반경 230~330mm 밖, z 150 초과, 관절1 125° 초과 좌표는 경고를 띄우고 한 번 더 물음.
      30mm 넘게 움직이는 직선 이동도 확인을 물음.
"""

import asyncio
import math
import os
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import dobot_control as dc
from worker_lock import WorkerLock

BIG_MOVE_MM = 30.0   # 이보다 크게 직선으로 움직이면 확인


def base_angle(x, y):
    return math.degrees(math.atan2(y, x))


def warnings_for(x, y, z, r):
    """좌표가 위험한 이유 목록"""
    out = []
    reach = math.hypot(x, y)
    if reach < dc.REACH_MIN:
        out.append(f"반경 {reach:.0f}mm 가 {dc.REACH_MIN:.0f}mm 안쪽 - 위로 들 때 알람 0x12")
    if reach > dc.REACH_MAX:
        out.append(f"반경 {reach:.0f}mm 가 {dc.REACH_MAX:.0f}mm 바깥 - 명령이 무시되기 쉬움")
    if z > dc.JUMP_Z_LIMIT:
        out.append(f"z {z:.0f} 가 JUMP 한계 {dc.JUMP_Z_LIMIT:.0f} 보다 높음")
    if abs(base_angle(x, y)) > dc.ANGLE_MAX:
        out.append(f"관절1 {base_angle(x, y):.0f}° 가 ±{dc.ANGLE_MAX:.0f}° 보다 큼 (한계 135°)")
    return out


async def read_pose(arm_api, port):
    res = await dc.safe_call(arm_api.GetPose(portName=port, isQueued=False))
    return res if isinstance(res, dict) and "x" in res else None


async def show(arm_api, port):
    p = await read_pose(arm_api, port)
    if p is None:
        print("!! 좌표를 못 읽음 (연결 확인)")
        return None
    reach = math.hypot(p["x"], p["y"])
    alarms = await dc.safe_call(arm_api.GetAlarmsState(portName=port), 3)
    codes = dc.alarm_codes(alarms)
    joints = p.get("jointAngle") or []
    print(f"  좌표 x={p['x']:.1f} y={p['y']:.1f} z={p['z']:.1f} r={p['r']:.1f}   반경 {reach:.1f}mm  베이스각 {base_angle(p['x'], p['y']):.1f}°"
          + (f"   관절 {[round(j, 1) for j in joints]}" if joints else ""))
    print("  알람: " + (", ".join(dc.describe_alarm(c) for c in codes) if codes else "없음"))
    return p


async def go(arm_api, port, cfg, pose, mode, ask, label):
    """이동 전 경고·확인 → 이동. 반환: 도착 여부"""
    why = warnings_for(*pose)
    for w in why:
        print(f"  !! {w}")
    if why:
        if ask("  그래도 이동할까요? (y/n): ").strip().lower() != "y":
            print("  취소")
            return False
    elif mode == dc.PTP_MODE_JUMP and ask(f"  [{label}] ({pose[0]:.1f},{pose[1]:.1f},{pose[2]:.1f},{pose[3]:.1f}) 로 올라갔다 옮겨서 내려갑니다. 이동할까요? (y/n): ").strip().lower() != "y":
        print("  취소")
        return False
    if mode == dc.PTP_MODE_JUMP:
        await dc.lower_if_too_high(arm_api, port)
        await dc.use_travel_height(arm_api, port, cfg, pose)
    _, ok, now = await dc.move_to(arm_api, port, pose, mode=mode, fallback=False)
    print(f"  [{label}] 목표=({pose[0]:.1f},{pose[1]:.1f},{pose[2]:.1f},{pose[3]:.1f}) 도착:{ok}")
    return ok


async def session(arm_api, port, cfg, ask=input, box_api=None):
    print(f"\n{port} 연결됨. 'pose' 로 좌표 보기, 'q' 로 끝. (팔이 실제로 움직이니 주변을 비우세요)")
    await show(arm_api, port)
    jump_cfg = cfg
    last_stack = [None]   # 마지막으로 pick 한 더미 (put 의 놓는 R 용)
    while True:
        line = ask("\n명령> ").strip().lower()
        if line in ("", "pose"):
            await show(arm_api, port)
            continue
        if line == "q":
            break
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "goto" and len(args) == 4:
                target = tuple(float(a) for a in args)
                await go(arm_api, port, jump_cfg, target, dc.PTP_MODE_JUMP, ask, "이동")
            elif cmd in ("dx", "dy", "dz", "dr", "out") and len(args) == 1:
                d = float(args[0])
                p = await read_pose(arm_api, port)
                if p is None:
                    print("!! 좌표를 못 읽음")
                    continue
                x, y, z, r = p["x"], p["y"], p["z"], p["r"]
                if cmd == "dx":
                    x += d
                elif cmd == "dy":
                    y += d
                elif cmd == "dz":
                    z += d
                elif cmd == "dr":
                    r += d
                else:  # out: 반경 방향
                    reach = math.hypot(x, y)
                    k = (reach + d) / reach if reach else 1.0
                    x, y = x * k, y * k
                moved = math.dist((p["x"], p["y"], p["z"]), (x, y, z))
                if moved > BIG_MOVE_MM and ask(f"  {moved:.0f}mm 직선으로 움직입니다. 괜찮습니까? (y/n): ").strip().lower() != "y":
                    print("  취소")
                    continue
                await go(arm_api, port, jump_cfg, (x, y, z, r), dc.PTP_MODE_MOVL, ask, "직선 이동")
            elif cmd == "slot" and len(args) == 2:
                s_no, floor = int(args[0]), int(args[1])
                stack = dc.tray_stock.STACKS[s_no - 1]
                x, y, z, r = stack[dc.tray_stock.FLOORS - floor]
                up = dc.TRAY.get("away_lift_z", dc.LIFT_Z_OFFSET)
                await go(arm_api, port, jump_cfg, (x, y, z + up, r), dc.PTP_MODE_JUMP, ask, f"더미{s_no} {floor}층 위")
            elif cmd == "cycle" and len(args) == 2:
                s_no, floor = int(args[0]), int(args[1])
                pick = dc.tray_stock.STACKS[s_no - 1][dc.tray_stock.FLOORS - floor]
                if ask(f"  더미{s_no} {floor}층 {pick} 에서 트레이를 집어 벨트에 놓습니다. 그 자리에 트레이가 있고 벨트 자리가 비었습니까? (y/n): ").strip().lower() != "y":
                    print("  취소")
                    continue
                solo = dict(dc.TRAY)
                solo["pickup_slots"] = dict(dc.TRAY["pickup_slots"])
                solo["pickup_slots"][1] = pick
                bx, by, bz, br = dc.TRAY["place_slots"][1]
                solo["place_slots"] = {1: (bx, by, bz, dc.tray_stock.place_r(s_no, br))}   # 더미별 놓는 R
                res = await dc.arm_set(arm_api, solo)
                print(f"  결과: {'성공' if res.get(1) else '실패'}  (개수는 안 뺌)")
            elif cmd == "pick" and len(args) == 2:
                s_no, floor = int(args[0]), int(args[1])
                x, y, z, r = dc.tray_stock.STACKS[s_no - 1][dc.tray_stock.FLOORS - floor]
                up = dc.TRAY.get("away_lift_z", dc.LIFT_Z_OFFSET)
                last_stack[0] = s_no   # put 이 이 더미의 놓는 R 을 쓰게
                if ask(f"  더미{s_no} {floor}층에서 트레이를 집습니다 (쥔 채로 둠). 진행할까요? (y/n): ").strip().lower() != "y":
                    print("  취소")
                    continue
                print(f"  그리퍼 열기: {await dc.gripper(arm_api, port, on=False)}")
                if (await go(arm_api, port, jump_cfg, (x, y, z + up, r), dc.PTP_MODE_JUMP, lambda *_: "y", "위로 이동")
                        and await go(arm_api, port, jump_cfg, (x, y, z, r), dc.PTP_MODE_MOVL, lambda *_: "y", "내려감")):
                    print(f"  쥐기: {await dc.gripper(arm_api, port, on=True)}")
                    await go(arm_api, port, jump_cfg, (x, y, z + up, r), dc.PTP_MODE_MOVL, lambda *_: "y", "올라옴")
                else:
                    print("  !! 못 갔음 - 쥐지 않고 멈춤")
            elif cmd == "put":
                x, y, z, r = dc.TRAY["place_slots"][1]
                r = dc.tray_stock.place_r(last_stack[0], r) if last_stack[0] else r
                if ask("  쥐고 있는 트레이를 벨트 놓는 자리에 놓습니다. 벨트 자리가 비었습니까? (y/n): ").strip().lower() != "y":
                    print("  취소")
                    continue
                if (await go(arm_api, port, jump_cfg, (x, y, z + 40.0, r), dc.PTP_MODE_JUMP, lambda *_: "y", "놓는 자리 위로")
                        and await go(arm_api, port, jump_cfg, (x, y, z, r), dc.PTP_MODE_MOVL, lambda *_: "y", "내려감")):
                    print(f"  놓기(그리퍼 열기): {await dc.gripper(arm_api, port, on=False)}")
                    await go(arm_api, port, jump_cfg, (x, y, z + 40.0, r), dc.PTP_MODE_MOVL, lambda *_: "y", "올라옴")
                else:
                    print("  !! 못 갔음 - 쥔 채로 멈춤. 손으로 받치세요")
            elif cmd == "place":
                x, y, z, r = dc.TRAY["place_slots"][1]
                await go(arm_api, port, jump_cfg, (x, y, z + 40.0, r), dc.PTP_MODE_JUMP, ask, "벨트 놓는 자리 위")
            elif cmd == "open":
                print(f"  그리퍼 열기: {await dc.gripper(arm_api, port, on=False)}")
            elif cmd == "close":
                if ask("  그리퍼를 닫습니다 (쥠). 괜찮습니까? (y/n): ").strip().lower() == "y":
                    print(f"  그리퍼 닫기: {await dc.gripper(arm_api, port, on=True)}")
            elif cmd in ("belt", "back") and len(args) <= 2:
                if box_api is None or "conveyor_index" not in cfg:
                    print("  이 팔에는 컨베이어 명령이 없습니다 (키캡 팔 set1·set2 전용. 트레이 벨트는 보드 쪽 board_conveyor_test.py)")
                    continue
                sec = float(args[0]) if args else dc.CONVEYOR_RUN_TIME
                base = float(args[1]) if len(args) > 1 else dc.CONVEYOR_SPEED
                reverse = (cmd == "back") != cfg.get("conveyor_reverse", False)   # 세트마다 원래 방향이 달라서 실제 운전 기준으로 맞춤
                if ask(f"  벨트(STEPPER{cfg['conveyor_index'] + 1})를 {sec:g}초 {'반대 방향' if cmd == 'back' else '정방향(실제 운전 방향)'}"
                       f"으로 돌립니다 (속도 {base:g}mm/s, 약 {base * sec:.0f}mm). 벨트 위가 비어 있습니까? (y/n): ").strip().lower() != "y":
                    print("  취소")
                    continue
                try:
                    print(f"  시작: {await dc.conveyor(box_api, port, cfg['conveyor_index'], True, -base if reverse else base)}")
                    await asyncio.sleep(sec)
                finally:
                    print(f"  정지: {await dc.conveyor(box_api, port, cfg['conveyor_index'], False, 0)}")
            elif cmd == "beltstop":
                if box_api is None or "conveyor_index" not in cfg:
                    print("  이 팔에는 컨베이어가 없습니다")
                    continue
                print(f"  정지: {await dc.conveyor(box_api, port, cfg['conveyor_index'], False, 0)}")
            elif cmd == "clear":
                await dc.clear_alarms_now(arm_api, port, "수동 해제")
                print("  알람 해제함")
            elif cmd == "home":
                if ask("  원점 복귀: 팔이 홈 위치로 움직입니다. 주변을 비웠습니까? (y/n): ").strip().lower() == "y":
                    await dc.clear_alarms_now(arm_api, port, "홈 전")
                    print(f"  홈 명령: {await dc.safe_call(arm_api.SetHOMECmd(portName=port, isQueued=False), 60)}")
                    last, still, start = None, None, time.time()
                    while time.time() - start < 60:
                        await asyncio.sleep(0.5)
                        now = await read_pose(arm_api, port)
                        if now and last and dc._same_pose(now, last):
                            still = still or time.time()
                            if time.time() - still >= 1.5:
                                break
                        else:
                            still = None
                        last = now or last
                    print("  홈 복귀 끝")
            else:
                print("  명령: pose  goto x y z r  dx/dy/dz/dr 값  out 값  slot 더미 층  place  cycle 더미 층  pick 더미 층  put  belt/back [초]  beltstop  open  close  clear  home  q")
                continue
        except (ValueError, IndexError, KeyError):
            print("  입력이 잘못됨 (예: goto 250 100 120 20 / dz 10 / slot 3 5)")
            continue
        await show(arm_api, port)


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "tray"
    if key != "tray" and key not in dc.SETS:
        raise SystemExit("사용법: python move_arm.py [tray | set1 | set2]")
    cfg = dc.TRAY if key == "tray" else dc.SETS[key]
    port = cfg["port"]
    if not port:
        raise SystemExit(f"{key} 팔 USB 가 안 보입니다 (전원·케이블 확인)")
    with WorkerLock():   # 게이트웨이가 켜져 있으면 여기서 멈춤
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
        arm_api = getattr(dobotlink, dc.NAMESPACE_ARM)
        print(f"{key} ({port}) 연결: {loop.run_until_complete(dc.safe_call(arm_api.ConnectDobot(portName=port)))}")
        try:
            loop.run_until_complete(session(arm_api, port, cfg, box_api=getattr(dobotlink, dc.NAMESPACE_BOX)))
        except KeyboardInterrupt:
            print("\n중단 - 팔 상태를 확인하세요")
        finally:
            if "conveyor_index" in cfg:  # Ctrl+C 로 벨트 도중 끊겨도 벨트는 세움
                loop.run_until_complete(dc.conveyor(getattr(dobotlink, dc.NAMESPACE_BOX), port, cfg["conveyor_index"], False, 0))
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
