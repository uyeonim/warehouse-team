# -*- coding: utf-8 -*-
"""
키캡 컨베이어 벨트만 움직이기 (대화형) - 로봇팔·아두이노는 안 움직임

  컨베이어 1 = 키캡 팔 세트1 벨트 (EI·NS 키캡이 오는 쪽),  컨베이어 2 = 세트2 벨트 (TF·PJ)

사용 (게이트웨이·보드 main 을 끄고 실행 - 같은 매직박스를 씀. 벨트가 실제로 돌음):
  python belt_run.py
    컨베이어> 1           <- 번호를 고르면
    컨베이어 1 을(를) 10초 동안 돌립니다 ... (y/n): y     <- y 로 10초 동안 돌고 멈춤
  번호: 1, 2, 또는 "1 2" (둘 다 동시에).  "1 back" 처럼 뒤에 back 을 붙이면 반대 방향.  q = 끝
  python belt_run.py 1 y                 인자로 바로 (확인 질문 없이): 컨베이어 1 을 10초
  python belt_run.py 1 2 --sec 5         둘 다 5초
  python belt_run.py 2 --speed 50        속도 지정 (mm/s)
기본: 10초, 속도는 실제 운전과 같음(dobot_control.CONVEYOR_SPEED), 방향은 실제 운전 방향 (세트마다 달라서 코드가 맞춤).
Ctrl+C 를 누르면 바로 멈춤.
"""
import asyncio
import sys

from DobotRPC import RPCClient, DobotlinkAdapter

import dobot_control as dc
from worker_lock import WorkerLock

DEFAULT_SEC = 10.0
BELT = {"1": "set1", "2": "set2"}   # 컨베이어 번호 -> 세트


def parse_choice(text):
    """'1' / '2' / '1 2' / '1 back' -> (번호 목록, 반대 방향 여부). 모르는 입력이면 ValueError"""
    tokens = text.replace(",", " ").split()
    reverse = any(t.lower() == "back" for t in tokens)
    nums = []
    for t in tokens:
        if t.lower() == "back":
            continue
        if t not in BELT:
            raise ValueError(f"모르는 입력: {t} (1, 2, 1 2, 뒤에 back 가능)")
        if t not in nums:
            nums.append(t)
    if not nums:
        raise ValueError("컨베이어 번호를 주세요 (1, 2, 또는 1 2)")
    return nums, reverse


async def run_belts(box_api, nums, reverse, sec, speed):
    """고른 벨트를 sec 초 동안 (동시에) 돌리고 멈춤. 반환: {번호: (시작 응답, 정지 응답)}"""
    cfgs = {n: dc.SETS[BELT[n]] for n in nums}
    for n, cfg in cfgs.items():
        if not cfg["port"]:
            raise RuntimeError(f"컨베이어 {n}: {BELT[n]} 팔 USB 가 안 보입니다 (전원·케이블 확인)")

    def signed(cfg):
        rev = reverse != cfg.get("conveyor_reverse", False)   # 세트마다 원래 방향이 달라서 실제 운전 기준으로 맞춤
        return -speed if rev else speed

    result = {}
    try:
        starts = await asyncio.gather(*[
            dc.conveyor(box_api, cfg["port"], cfg["conveyor_index"], True, signed(cfg)) for cfg in cfgs.values()])
        for n, s in zip(cfgs, starts):
            print(f"  컨베이어 {n} 시작: {s}")
        await asyncio.sleep(sec)
    finally:
        stops = await asyncio.gather(*[
            dc.conveyor(box_api, cfg["port"], cfg["conveyor_index"], False, 0) for cfg in cfgs.values()])
        for n, s, e in zip(cfgs, starts if "starts" in locals() else [None] * len(cfgs), stops):
            print(f"  컨베이어 {n} 정지: {e}")
            result[n] = (s, e)
    return result


async def interactive(box_api, sec, speed, ask=input, first=None):
    while True:
        line = first if first is not None else ask("\n컨베이어> ").strip()
        first = None
        if line.lower() in ("q", "quit", "exit"):
            return
        if not line:
            continue
        try:
            nums, reverse = parse_choice(line)
        except ValueError as e:
            print("  " + str(e))
            continue
        names = " + ".join(f"컨베이어 {n}(STEPPER{dc.SETS[BELT[n]]['conveyor_index'] + 1})" for n in nums)
        direction = "반대 방향" if reverse else "정방향(실제 운전 방향)"
        if ask(f"{names} 을(를) {sec:g}초 동안 {direction}으로 돌립니다 (속도 {speed:g}mm/s, 약 {speed * sec:.0f}mm). "
               f"벨트 위가 비어 있습니까? (y/n): ").strip().lower() != "y":
            print("  취소")
            continue
        try:
            await run_belts(box_api, nums, reverse, sec, speed)
            print("  끝. 키캡이 멈춘 자리를 확인하세요.")
        except RuntimeError as e:
            print("  !! " + str(e))


def main():
    argv = sys.argv[1:]
    sec, speed = DEFAULT_SEC, dc.CONVEYOR_SPEED
    for flag in ("--sec", "--speed"):
        if flag in argv:
            k = argv.index(flag)
            try:
                value = float(argv[k + 1])
            except (IndexError, ValueError):
                raise SystemExit(f"사용법: {flag} 숫자")
            if flag == "--sec":
                sec = value
            else:
                speed = value
            del argv[k:k + 2]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    direct = None
    if argv:   # 인자로 바로: "1" / "1 2" / "1 y" (y 는 확인 생략)
        yes = argv[-1].lower() == "y"
        if yes:
            argv = argv[:-1]
        try:
            nums, reverse = parse_choice(" ".join(argv))
        except ValueError as e:
            raise SystemExit(str(e))
        direct = (nums, reverse, yes)

    with WorkerLock():   # 게이트웨이가 켜져 있으면 여기서 멈춤
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
        arm_api = getattr(dobotlink, dc.NAMESPACE_ARM)
        box_api = getattr(dobotlink, dc.NAMESPACE_BOX)
        ports = sorted({dc.SETS[s]["port"] for s in BELT.values() if dc.SETS[s]["port"]})
        for port in ports:
            print(f"{port} 연결: {loop.run_until_complete(dc.safe_call(arm_api.ConnectDobot(portName=port)))}")
        try:
            if direct:
                nums, reverse, yes = direct
                if not yes:
                    loop.run_until_complete(interactive(box_api, sec, speed, first=" ".join(nums) + (" back" if reverse else "")))
                else:
                    loop.run_until_complete(run_belts(box_api, nums, reverse, sec, speed))
            else:
                print("키캡 컨베이어 (팔·아두이노는 안 움직임). 1 = 세트1, 2 = 세트2, '1 2' = 둘 다, 뒤에 back = 반대 방향, q = 끝")
                loop.run_until_complete(interactive(box_api, sec, speed))
        except KeyboardInterrupt:
            print("\n중단 - 벨트를 세웁니다")
            for s in BELT.values():
                cfg = dc.SETS[s]
                if cfg["port"]:
                    loop.run_until_complete(dc.conveyor(box_api, cfg["port"], cfg["conveyor_index"], False, 0))
        finally:
            for port in ports:
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
