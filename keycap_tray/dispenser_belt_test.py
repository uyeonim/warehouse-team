# -*- coding: utf-8 -*-
"""
키캡 배출 + 컨베이어 벨트 시험 (하나로 통합) - 키캡이 물리적으로 잘 나오는지만 보는 단순 도구.
재고·주문·키캡 Flask·Mobius·IR 판정은 전혀 안 씀 (그래서 칸이 disable 로 꺼지지도 않음).

사용:  python dispenser_belt_test.py     (창고 끄기 없이 바로 됨)

  명령> E                  알파벳을 입력하고 y -> 그 알파벳 4색(핑크·옐로우·블루·그린) 서보가 차례로 밀어 배출
                           (끝나면 "컨베이어 N 을 10초 돌릴까요?" 도 y 로)
  명령> belt 1             컨베이어 1 만 10초  (y)
  명령> belt 2             컨베이어 2 만 10초  (y)
  명령> belt 1 2           둘 다 동시에 10초  (y)
  명령> belt 1 back        반대 방향
  q                        끝

  알파벳: E I N S T F P J / 여러 개 "E N" / 전부 all / 칸 하나 E_r.   컨베이어 1 = E I N S (세트1), 컨베이어 2 = T F P J (세트2)
  옵션: --hold 1.5 (푸시 머무는 초) --gap 2.5 (칸 사이 쉬는 초) --sec 10 (벨트 초) --noBelt (배출 뒤 벨트 질문 생략) --dry (계획만)

원리: 서보 영점 조절(CALIB) 명령을 MQTT 로 직접 보냄 -> 그 칸 서보가 푸시 위치로 갔다가 대기 위치로 돌아옴
      (상태판 '서보 영점 맞추기' 의 푸시 -> 대기와 같음). 벨트는 매직박스를 직접 돌림.
준비: MQTT(192.168.0.100:1883) 와 아두이노가 켜져 있으면 됨. 키캡 Flask 는 필요 없음.
      창고 프로그램이 켜져 있어도(창고 끄기 안 해도) 됨 - 벨트는 공유 모드로 돌림. 다만 주문이 진행 중일 때는 쓰지 말 것.
주의: 서보 푸시만 하는 것이라 키캡이 걸려 있으면 그대로 밀려 나옴. 아두이노 재고 기록은 안 줄어듦(Flask 를 안 거치므로).
"""
import asyncio
import json
import sys
import time

import dispense_test as dt   # 알파벳·칸 입력 해석(expand), MQTT 접속 정보(broker) 재사용

TOPIC_COMMAND = "sjuiot/warehouse/command"   # 복구·영점 명령 토픽 (4대 공통 구독, retain 없이 발행)
SET_OF_BOARD = {"EI": "1", "NS": "1", "TF": "2", "PJ": "2"}   # 보드 -> 키캡 벨트(컨베이어 번호)


def servo_steps(rails, hold, gap):
    """칸 목록 -> [(칸, 위치, 그 뒤 기다릴 초)] : 푸시 -> hold 초 -> 대기 -> gap 초 를 칸마다"""
    steps = []
    for r in rails:
        steps.append((r, "push", hold))
        steps.append((r, "home", gap))
    return steps


def belts_needed(rails):
    """칸 목록 -> 필요한 컨베이어 번호 (보드가 속한 세트 기준)"""
    return sorted({SET_OF_BOARD[dt.AXES[dt.axis_of(r[0])]] for r in rails})


def make_client():
    import paho.mqtt.client as mqtt
    host, port = dt.broker()
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="dispenser_belt_test")
    except AttributeError:   # paho 1.x
        client = mqtt.Client(client_id="dispenser_belt_test")
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


def send_calib(client, rail, pos):
    info = client.publish(TOPIC_COMMAND, json.dumps({"cmd": "CALIB", "rail": rail, "pos": pos}), qos=1, retain=False)
    info.wait_for_publish(timeout=5)
    return info.is_published()


def run_servos(client, rails, hold, gap, send=None, sleep=None):
    send = send or send_calib
    sleep = sleep or time.sleep
    for i, (rail, pos, wait) in enumerate(servo_steps(rails, hold, gap)):
        ok = send(client, rail, pos)
        label = "푸시(밀어냄)" if pos == "push" else "대기로 복귀"
        print(f"  {rail}  {label}  {'보냄' if ok else '!! 못 보냄 (MQTT 확인)'}")
        sleep(wait)


def run_belt(nums, sec, reverse=False):
    """컨베이어 벨트를 sec 초 돌림.
    창고 프로그램(게이트웨이)이 꺼져 있으면 평소처럼 잠금을 잡고 연결·해제까지 함.
    켜져 있어도 거부하지 않고 '공유 모드'로 돌림: DobotLink 는 같은 매직박스에 여러 프로그램이 명령을 보낼 수 있어서
    (이미 열려 있다는 경고는 무시) 벨트 명령만 보내고, 연결 해제는 안 함 (해제하면 게이트웨이 연결이 끊겨 재연결함).
    단, 주문이 진행 중일 때 쓰면 게이트웨이의 벨트 동작과 섞임 - 주문이 없을 때만."""
    from DobotRPC import RPCClient, DobotlinkAdapter
    import belt_run as br
    import dobot_control as dc
    from worker_lock import WorkerLock
    shared = False
    try:
        lock = WorkerLock()
        lock.__enter__()
    except RuntimeError:
        shared, lock = True, None
        print("  (창고 프로그램이 켜져 있는 채로 벨트만 돌립니다 - 주문이 진행 중이 아닐 때만 쓰세요)")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
    arm_api = getattr(dobotlink, dc.NAMESPACE_ARM)
    box_api = getattr(dobotlink, dc.NAMESPACE_BOX)
    ports = sorted({dc.SETS[br.BELT[n]]["port"] for n in nums if dc.SETS[br.BELT[n]]["port"]})
    try:
        for port in ports:
            res = loop.run_until_complete(dc.safe_call(arm_api.ConnectDobot(portName=port)))
            if not shared:
                print(f"  {port} 연결: {res}")   # 공유 모드의 '이미 열려 있음' 경고는 정상이라 안 보여 줌
        loop.run_until_complete(br.run_belts(box_api, nums, reverse, sec, dc.CONVEYOR_SPEED))
        return True
    except RuntimeError as e:
        print("  !! " + str(e))
        return False
    except KeyboardInterrupt:
        print("\n중단 - 벨트를 세웁니다")
        for n in nums:
            cfg = dc.SETS[br.BELT[n]]
            if cfg["port"]:
                loop.run_until_complete(dc.conveyor(box_api, cfg["port"], cfg["conveyor_index"], False, 0))
        return False
    finally:
        if not shared:   # 공유 모드에서는 연결을 끊지 않음 (게이트웨이가 쓰는 연결)
            for port in ports:
                loop.run_until_complete(dc.safe_call(arm_api.DisconnectDobot(portName=port)))
            lock.__exit__(None, None, None)


def belt_only(line, opts, ask=input, belt=run_belt):
    """'belt 1' / 'belt 1 2' / 'belt 2 back' -> 확인(y/n) 후 벨트만 돌림"""
    import belt_run as br
    nums, reverse = br.parse_choice(line)
    names = " + ".join(f"컨베이어 {n}" for n in nums)
    direction = "반대 방향" if reverse else "정방향(실제 운전 방향)"
    if opts["dry"]:
        print(f"  {names} 을(를) {opts['sec']:g}초 {direction}으로 돌립니다 (--dry: 돌리지 않음)")
        return
    if ask(f"  {names} 을(를) {opts['sec']:g}초 동안 {direction}으로 돌립니다. 벨트 위가 비어 있습니까? (y/n): ").strip().lower() != "y":
        print("  취소")
        return
    if belt(nums, opts["sec"], reverse):
        print("  벨트 끝. 키캡이 멈춘 자리를 확인하세요.")


def is_belt_command(line):
    word = line.strip().split(None, 1)[0].lower() if line.strip() else ""
    return word in ("belt", "b", "컨베이어")


def handle(line, opts, ask=input, client_factory=make_client, belt=run_belt):
    if is_belt_command(line):
        rest = line.strip().split(None, 1)[1] if len(line.strip().split(None, 1)) > 1 else ""
        belt_only(rest, opts, ask=ask, belt=belt)
        return
    rails = dt.expand(line.replace(",", " ").split())
    if not rails:
        return
    steps = len(rails) * (opts["hold"] + opts["gap"])
    print(f"  {dt.describe(rails)} = {len(rails)}칸 서보를 차례로 밀어 봅니다 (칸마다 푸시 {opts['hold']:g}초 + 쉬기 {opts['gap']:g}초, 약 {steps:.0f}초)")
    print("  (재고·Flask 안 씀. 서보만 움직임)")
    if opts["dry"]:
        print("  (--dry: 보내지 않음)")
        return
    if ask("  진행할까요? 서보가 실제로 움직여 키캡이 밀려 나옵니다. (y/n): ").strip().lower() != "y":
        print("  취소")
        return
    client = client_factory()
    try:
        run_servos(client, rails, opts["hold"], opts["gap"])
    finally:
        client.loop_stop()
        client.disconnect()
    print("  서보 끝. 키캡이 벨트에 떨어졌는지 보세요.")
    if opts["no_belt"]:
        return
    nums = belts_needed(rails)
    names = " + ".join(f"컨베이어 {n}" for n in nums)
    if ask(f"  {names} 을(를) {opts['sec']:g}초 돌릴까요? (벨트에 떨어진 키캡이 끝막이까지 가는지 확인) (y/n): ").strip().lower() == "y":
        belt(nums, opts["sec"], False)
        print("  벨트 끝.")


def main():
    argv = sys.argv[1:]
    opts = {"hold": 1.5, "gap": 2.5, "sec": 10.0, "no_belt": "--noBelt" in argv, "dry": "--dry" in argv}
    argv = [a for a in argv if a not in ("--noBelt", "--dry")]
    for flag, key in (("--hold", "hold"), ("--gap", "gap"), ("--sec", "sec")):
        if flag in argv:
            k = argv.index(flag)
            try:
                opts[key] = float(argv[k + 1])
            except (IndexError, ValueError):
                raise SystemExit(f"사용법: {flag} 숫자")
            del argv[k:k + 2]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    if argv:   # 인자로 바로 (확인 질문은 그대로)
        try:
            handle(" ".join(argv), opts)
        except ValueError as e:
            raise SystemExit(str(e))
        return
    print("키캡 배출 + 컨베이어 벨트 시험 (재고·Flask 안 씀).")
    print("  배출: 알파벳 E I N S T F P J (여러 개 'E N', 전부 'all', 칸 하나 'E_r') 입력 후 y")
    print("  벨트: 'belt 1' / 'belt 2' / 'belt 1 2' 입력 후 y (뒤에 back = 반대 방향).   q = 끝")
    while True:
        try:
            line = input("\n명령> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in ("q", "quit", "exit"):
            break
        if not line:
            continue
        try:
            handle(line, opts)
        except ValueError as e:
            print("  " + str(e))
        except KeyboardInterrupt:
            print("\n중단")


if __name__ == "__main__":
    main()
