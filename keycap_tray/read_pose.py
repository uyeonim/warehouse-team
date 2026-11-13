# -*- coding: utf-8 -*-
"""
Dobot 좌표 다시 찍기 (로봇을 움직이지 않음 — 좌표 읽기만)

실행:  py -3.9 read_pose.py set1     → 세트1 (COM6)
       py -3.9 read_pose.py set2     → 세트2 (COM7)
       py -3.9 read_pose.py set1 belt     → 컨베이어만 실제처럼 CONVEYOR_RUN_TIME(6초) 돌리고 멈춤 (멈춘 자리 확인용)
       py -3.9 read_pose.py set1 belt 6   → 6초 대신 다른 시간으로 (시간 바꿔 보기)
       py -3.9 read_pose.py set1 back     → 반대 방향으로 돌리기 (키캡 되돌리기용, 시간도 붙일 수 있음)
       py -3.9 read_pose.py set1 sw       → 벨트 시간 재기 (Enter 출발 / Enter 정지, 여러 번, q 끝)

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
import shutil
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


def stopwatch_belt(loop, box_api, arm_api, cfg, speed_mm_s=None):
    """[11/11] 스톱워치: Enter로 출발, 키캡이 자리에 오면 Enter로 정지 -> 걸린 시간. 여러 번 재고 q로 끝.
    실제 운전과 같은 방향·속도 (conveyor_index / conveyor_reverse 반영). 시간 = 시작 명령 응답 후 ~ 정지 명령 전"""
    port = cfg["port"]
    base = dc.CONVEYOR_SPEED if speed_mm_s is None else speed_mm_s
    speed = -base if cfg.get("conveyor_reverse", False) else base
    times = []
    print(f"스톱워치 (STP{cfg['conveyor_index'] + 1}, {base} mm/s{', 반대 방향' if speed < 0 else ''}). "
          f"지금 코드 CONVEYOR_RUN_TIME = {dc.CONVEYOR_RUN_TIME}초")
    try:
        while True:
            if input("\nEnter = 출발, q = 끝: ").strip().lower() == "q":
                break
            print(f"  시작: {loop.run_until_complete(dc.conveyor(box_api, port, cfg['conveyor_index'], True, speed))}")
            t0 = time.time()
            try:
                input("  키캡이 멈출 자리에 오면 Enter")
            finally:
                elapsed = time.time() - t0
                print(f"  정지: {loop.run_until_complete(dc.conveyor(box_api, port, cfg['conveyor_index'], False, 0))}")
            times.append(elapsed)
            print(f"  -> {elapsed:.1f}초 (약 {elapsed * base:.0f} mm)")
    except (KeyboardInterrupt, EOFError):
        print("\n중간에 멈춤")
    finally:
        loop.run_until_complete(dc.conveyor(box_api, port, cfg["conveyor_index"], False, 0))
        loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
    if times:
        print("\n잰 시간: " + ", ".join(f"{t:.1f}" for t in times) + " 초"
              f"  (Enter 반응만큼 0.2~0.3초 길게 나올 수 있음)")


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

    if len(sys.argv) > 2 and sys.argv[2] == "sw":
        # py -3.9 read_pose.py set1 sw [속도mm/s]  -> 벨트 시간 재기 (스톱워치)
        stopwatch_belt(loop, getattr(dobotlink, dc.NAMESPACE_BOX), arm_api, cfg,
                       speed_mm_s=float(sys.argv[3]) if len(sys.argv) > 3 else None)
        return

    if len(sys.argv) > 2 and sys.argv[2] in ("belt", "back"):
        # py -3.9 read_pose.py set1 belt [초] [속도mm/s]
        run_belt(loop, getattr(dobotlink, dc.NAMESPACE_BOX), arm_api, cfg,
                 float(sys.argv[3]) if len(sys.argv) > 3 else dc.CONVEYOR_RUN_TIME,
                 reverse=sys.argv[2] == "back",
                 speed_mm_s=float(sys.argv[4]) if len(sys.argv) > 4 else None)
        return

    new ={"pickup_slots": dict(cfg["pickup_slots"]), "place_slots": dict(cfg["place_slots"])}
    points = TRAY_POINTS if set_key == "tray" else POINTS
    # 트레이 더미(pose_tray.json stacks)를 쓰는 중이면 순서: 더미 1·2·3 의 5층 → 더미 1·2·3 의 4층 → 놓는 곳 (11/12).
    # 3·2·1층은 tray_stock.py 가 (4층 - 5층) 차이를 더미마다 따로 더해서 계산. (예전엔 집는 곳 하나만 찍고 파일을 통째로
    # 새로 써서 stacks·pitch·travel_z 가 지워졌음 → 15개 기능이 꺼짐, 2026-11-10)
    stacks = dc.tray_stock.STACKS if set_key == "tray" else []
    if stacks:
        floors = dc.tray_stock.FLOORS
        new["stacks"] = {i + 1: stack[0] for i, stack in enumerate(stacks)}
        new["stacks4"] = {i + 1: stack[1] for i, stack in enumerate(stacks)}
        points = ([("stacks", i + 1, f"더미{i + 1} 맨 위({floors}층) 손잡이 — {floors}개 다 쌓은 상태에서")
                   for i in range(len(stacks))] +
                  [("stacks4", i + 1, f"더미{i + 1} {floors - 1}층 손잡이 — 맨 위 트레이를 치우고 {floors - 1}개 남은 상태에서")
                   for i in range(len(stacks))] + [TRAY_POINTS[1]])
    if set_key == "tray":   # [11/14] 트레이 팔 쉬는 자리 (일 끝나면 가 있을 곳) - 건너뛰면(s) 안 정함/기존 유지
        new["home"] = {1: cfg.get("home")}
        points = list(points) + [("home", 1, "트레이 팔 쉬는 자리(HOME) — 일 끝나고 가 있을 빈 곳, 트레이·더미에서 떨어진 높은 곳")]
    before = {g: dict(d) for g, d in new.items()}
    try:
        for group, slot, label in points:
            old = new[group][slot] or "아직 없음"
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
    try:  # 파일에 있던 다른 값(_note, pitch, travel_z 등)은 그대로 두고 찍은 좌표만 바꿈
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if stacks:
        for i in range(len(stacks)):  # 새로 찍은 더미만 바꿈 (건너뛴 더미는 그대로)
            top, second = new["stacks"][i + 1], new["stacks4"][i + 1]
            top_changed = top != before["stacks"][i + 1]
            second_changed = second != before["stacks4"][i + 1]
            if not top_changed and not second_changed:
                continue
            if top_changed and not second_changed:  # 5층만 새로 찍음 -> 4층은 예전 차이를 그대로 적용
                old_top, old_second = before["stacks"][i + 1], before["stacks4"][i + 1]
                second = tuple(round(a + (s - o), 1) for a, s, o in zip(top, old_second, old_top))
                new["stacks4"][i + 1] = second
                print(f"  (더미{i + 1} 4층은 안 찍어서 예전 차이로 계산: {second})")
            saved["stacks"][i] = [list(top), list(second)]
        new["pickup_slots"][1] = new["stacks"][1]  # 더미를 쓸 때 집는 곳은 매번 남은 개수로 정함. 여기는 더미1 맨 위로 맞춰만 둠
    for group in ("pickup_slots", "place_slots"):
        saved[group] = {str(k): v for k, v in new[group].items()}
    if set_key == "tray" and new["home"][1]:
        saved["home"] = list(new["home"][1])
    if os.path.exists(path):   # [11/11] 덮어쓰기 전에 예전 파일 백업 (pose_<이름>.json.bak-날짜시간, git 무시)
        backup = path + time.strftime(".bak-%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        print(f"\n예전 파일 백업: {os.path.basename(backup)}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=2, ensure_ascii=False)
    print(f"\n===== {set_key} 새 좌표 (저장: {os.path.basename(path)}) =====")
    for group in ((("stacks", "stacks4", "place_slots") if stacks else ("pickup_slots", "place_slots"))
                  + (("home",) if set_key == "tray" else ())):
        for slot in sorted(new[group]):
            mark = "" if new[group][slot] == before[group][slot] else "   ← 바뀜"
            print(f"  {group}[{slot}] = {new[group][slot]}{mark}")
    if stacks:
        print("\n  더미별 층당 차이 (4층 - 5층 = x, y, z, r). z 는 -25 ~ -33 쯤, x·y·r 은 0 에 가까워야 정상")
        for i in range(len(stacks)):
            d = tuple(round(b - a, 1) for a, b in zip(new["stacks"][i + 1], new["stacks4"][i + 1]))
            warn = "" if -40 <= d[2] <= -20 else "   !! z 차이가 이상함 - 5층·4층을 제대로 찍었는지 확인"
            print(f"    더미{i + 1}: {d}{warn}")


if __name__ == "__main__":
    main()
