# -*- coding: utf-8 -*-
"""
Dobot 좌표 다시 찍기 (로봇을 움직이지 않음 — 좌표 읽기만)

실행:  py -3.9 read_pose.py set1     → 세트1 (COM6)
       py -3.9 read_pose.py set2     → 세트2 (COM7)
       py -3.9 read_pose.py set1 belt     → 컨베이어만 실제처럼 CONVEYOR_RUN_TIME(6초) 돌리고 멈춤 (멈춘 자리 확인용)
       py -3.9 read_pose.py set1 belt 6   → 6초 대신 다른 시간으로 (시간 바꿔 보기)
       py -3.9 read_pose.py set1 back     → 반대 방향으로 돌리기 (키캡 되돌리기용, 시간도 붙일 수 있음)

방법
  1. 로봇팔을 손으로 원하는 위치로 옮김 (팔의 잠금해제 버튼을 누른 채로 움직이기)
  2. Enter → 지금 좌표를 저장.  s + Enter → 이 위치는 건너뜀(기존 좌표 유지)
  3. 4군데(집는 곳 2 + 놓는 곳 2)를 다 찍으면 새 좌표를 보여주고 pose_<세트>.json 에 저장
     → 그 파일 내용을 dobot_control.py 의 SETS 에 반영

※ DobotLink 가 켜져 있어야 함. 게이트웨이(local_gateway.py)가 같은 로봇을 쓰는 중이면 먼저 끄기.
"""

import asyncio
import json
import os
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import dobot_control as dc

POINTS = [("pickup_slots", 1, "슬롯1 집는 곳 (벨트 위 키캡)"),
          ("pickup_slots", 2, "슬롯2 집는 곳 (벨트 위 키캡)"),
          ("place_slots", 1, "슬롯1 놓는 곳"),
          ("place_slots", 2, "슬롯2 놓는 곳")]
TRAY_POINTS = [("pickup_slots", 1, "빈 트레이 집는 곳 (쌓아둔 곳)"),
               ("place_slots", 1, "트레이 놓는 곳 (벨트 위)")]


async def get_pose(arm_api, port):
    res = await dc.safe_call(arm_api.GetPose(portName=port, isQueued=False))
    if isinstance(res, dict) and "x" in res:
        return tuple(round(res[k], 1) for k in ("x", "y", "z", "r"))
    return None


def run_belt(loop, box_api, arm_api, cfg, seconds, reverse=False, speed_mm_s=None):
    """컨베이어만 seconds 초 돌리고 멈춤 (로봇팔은 안 움직임).
    reverse=True 면 반대 방향, speed_mm_s 를 주면 그 속도로 (안 주면 실제 운전과 같은 CONVEYOR_SPEED)."""
    port = cfg["port"]
    reverse = reverse != cfg.get("conveyor_reverse", False)  # 세트2 처럼 원래 반대로 도는 벨트면 back 은 정방향
    base = dc.CONVEYOR_SPEED if speed_mm_s is None else speed_mm_s
    speed = -base if reverse else base
    print(f"컨베이어만 {seconds}초 구동{' (반대 방향)' if reverse else ''} "
          f"(속도 {base} mm/s, 약 {base * seconds:.0f} mm 이동)")
    try:
        print(f"  시작: {loop.run_until_complete(dc.conveyor(box_api, port, cfg['conveyor_index'], True, speed))}")
        loop.run_until_complete(asyncio.sleep(seconds))
    except KeyboardInterrupt:
        print("  중간에 멈춤")
    finally:
        print(f"  정지: {loop.run_until_complete(dc.conveyor(box_api, port, cfg['conveyor_index'], False, 0))}")
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
    print("키캡이 멈춘 자리를 확인한 뒤, 그 위치로 로봇팔 좌표를 찍으세요.")


def main():
    set_key = sys.argv[1] if len(sys.argv) > 1 else ""
    if set_key not in dc.SETS and set_key != "tray":
        print("사용법: py -3.9 read_pose.py set1   (set2 / tray 도 가능)")
        return
    cfg = dc.TRAY if set_key == "tray" else dc.SETS[set_key]
    port = cfg["port"]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
    arm_api = getattr(dobotlink, dc.NAMESPACE_ARM)
    print(f"{set_key} ({port}) 연결: {loop.run_until_complete(dc.safe_call(arm_api.ConnectDobot(portName=port)))}")

    if len(sys.argv) > 2 and sys.argv[2] == "one":
        # 키캡 1개만 집어서 놓기까지 (벨트는 안 돌림). 예: py -3.9 read_pose.py set1 one 1
        slot = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        solo = dict(cfg)
        solo["keycaps"] = [k for k in cfg["keycaps"] if k[1] == slot]
        print(f"{set_key} 슬롯{slot} 만 집기 → 놓기 (다른 팔·벨트는 안 움직임)")
        res = loop.run_until_complete(dc.arm_set(arm_api, solo))
        print("  결과:", {no: ("성공" if ok else "실패") for no, ok in res.items()})
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
        return

    if len(sys.argv) > 2 and sys.argv[2] == "pick":
        # 키캡 1개만 집어 보기 (놓지는 않음). 예: py -3.9 read_pose.py set1 pick 1
        slot = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        target = cfg["pickup_slots"][slot]
        print(f"슬롯{slot} 집는 자리 {target} 로 이동 → 집기 (놓지는 않음)")
        loop.run_until_complete(dc.gripper(arm_api, port, on=False))
        _, ok, pose = loop.run_until_complete(dc.move_to(arm_api, port, target, fallback=False))
        print(f"  도착: {ok}  현재 좌표: {pose}")
        print(f"  집기: {loop.run_until_complete(dc.gripper(arm_api, port, on=True))}")
        print("  (그리퍼는 쥔 채로 둡니다. 풀려면: py -3.9 read_pose.py " + set_key + " open)")
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
        return

    if len(sys.argv) > 2 and sys.argv[2] == "open":
        print(f"  그리퍼 열기: {loop.run_until_complete(dc.gripper(arm_api, port, on=False))}")
        loop.run_until_complete(dc.safe_call(arm_api.SetEndEffectorGripper(portName=port, enable=False, on=False, isQueued=False)))
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
        return

    if len(sys.argv) > 2 and sys.argv[2] == "clear":
        # 충돌 등으로 LED 가 빨간색(알람)일 때: 알람 확인 → 해제 → 다시 확인. 로봇은 안 움직임
        call = lambda c: loop.run_until_complete(dc.safe_call(c))
        print(f"  알람 상태(해제 전): {call(arm_api.GetAlarmsState(portName=port))}")
        print(f"  알람 해제: {call(arm_api.ClearAllAlarmsState(portName=port))}")
        print(f"  알람 상태(해제 후): {call(arm_api.GetAlarmsState(portName=port))}")
        print(f"  현재 좌표: {call(arm_api.GetPose(portName=port, isQueued=False))}")
        call(arm_api.DisconnectDobot(portName=port))
        return

    if len(sys.argv) > 2 and sys.argv[2] == "home":
        # 원점 복귀(homing) — 로봇팔이 실제로 움직임. 알람 해제 → 홈 명령 → 멈출 때까지 대기
        call = lambda c, t=dc.CALL_TIMEOUT: loop.run_until_complete(dc.safe_call(c, t))
        print(f"  알람 해제: {call(arm_api.ClearAllAlarmsState(portName=port))}")
        print(f"  홈 명령: {call(arm_api.SetHOMECmd(portName=port, isQueued=False), 60)}")
        prev, still_since, start = None, None, time.time()
        while time.time() - start < 60:
            pose = call(arm_api.GetPose(portName=port, isQueued=False))
            if isinstance(pose, dict) and prev and dc._same_pose(pose, prev):
                still_since = still_since or time.time()
                if time.time() - start > 5 and time.time() - still_since > 2:
                    break
            else:
                still_since = None
            prev = pose
            time.sleep(0.3)
        print(f"  홈 끝 (현재 좌표: {prev})")
        print(f"  알람 상태: {call(arm_api.GetAlarmsState(portName=port))}")
        call(arm_api.DisconnectDobot(portName=port))
        return

    if len(sys.argv) > 2 and sys.argv[2] in ("belt", "back"):
        # py -3.9 read_pose.py set1 belt [초] [속도mm/s]
        run_belt(loop, getattr(dobotlink, dc.NAMESPACE_BOX), arm_api, cfg,
                 float(sys.argv[3]) if len(sys.argv) > 3 else dc.CONVEYOR_RUN_TIME,
                 reverse=sys.argv[2] == "back",
                 speed_mm_s=float(sys.argv[4]) if len(sys.argv) > 4 else None)
        return

    new = {"pickup_slots": dict(cfg["pickup_slots"]), "place_slots": dict(cfg["place_slots"])}
    try:
        for group, slot, label in (TRAY_POINTS if set_key == "tray" else POINTS):
            old = cfg[group][slot] or "아직 없음"
            while True:
                ans = input(f"\n[{label}] 로봇팔을 옮긴 뒤 Enter (건너뛰기 s)  기존={old}: ").strip().lower()
                if ans == "s":
                    print("  → 기존 좌표 유지")
                    break
                pose = loop.run_until_complete(get_pose(arm_api, port))
                if pose is None:
                    print("  !! 좌표를 못 읽음 — 연결 확인 후 다시 Enter")
                    continue
                new[group][slot] = pose
                print(f"  → 저장: {pose}")
                break
    except KeyboardInterrupt:
        print("\n중단 — 저장하지 않음")
        return
    finally:
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"pose_{set_key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({g: {str(k): v for k, v in d.items()} for g, d in new.items()}, f, indent=2)
    print(f"\n===== {set_key} 새 좌표 (저장: {os.path.basename(path)}) =====")
    for group in ("pickup_slots", "place_slots"):
        for slot in sorted(new[group]):
            mark = "" if new[group][slot] == cfg[group][slot] else "   ← 바뀜"
            print(f"  {group}[{slot}] = {new[group][slot]}{mark}")


if __name__ == "__main__":
    main()
