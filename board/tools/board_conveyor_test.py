# -*- coding: utf-8 -*-
"""
트레이 컨베이어 단독 구동 테스트 - 로봇암은 안 움직임.
메인과 같은 방식으로 STP1 + STP2(벨트 2개)를 동시에 켜고, 지정 시간 뒤 정지.

사용: python tools/board_conveyor_test.py [초|sw|s0|s1|s2] [속도mm/s] [back]
  python tools/board_conveyor_test.py s1           -> section1 을 board_config 에 적힌 시간·속도로 (실제 주문 때와 똑같이 한 번)
  python tools/board_conveyor_test.py s0 / s2      -> section0 / section2 (s0 = 키캡 팔 앞, s1 = 보드 팔 앞, s2 = 픽업대)
  python tools/board_conveyor_test.py 5 70 back    -> 5초, 70mm/s, 반대 방향 (트레이를 되돌릴 때. 마지막에 back)
  python tools/board_conveyor_test.py          -> 스톱워치: Enter로 출발, 트레이가 자리에 오면 Enter로 정지 -> 걸린 시간 출력
                                                  (여러 구간 연달아 재고 q로 끝내면 전부 다시 출력)
  python tools/board_conveyor_test.py 5        -> 5초
  python tools/board_conveyor_test.py 5 30     -> 5초, 30mm/s
  python tools/board_conveyor_test.py sw 30    -> 스톱워치, 30mm/s
스톱워치 시간 = 메인과 같은 기준(구동 명령 응답 후 ~ 정지 명령 전) -> board_config의
TRAY_CONVEYOR_RUN_TIME_SECTION* 에 그대로 넣으면 됨 (Enter 누르는 반응만큼 0.2~0.3초 길게 나올 수 있음)

!! 메인(main_board_robot_arm.py) 끄고 실행할 것 - 같은 포트(Magic Box)를 씀
"""

import asyncio
import math
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # 루트의 board_config.py
from board_config import (
    DOBOT_PORT, TRAY_CONVEYOR_STP_1, TRAY_CONVEYOR_STP_2,
    TRAY_CONVEYOR_SPEED_MM_S, TRAY_CONVEYOR_RUN_TIME_SECTION0,
    TRAY_CONVEYOR_RUN_TIME_SECTION1, TRAY_CONVEYOR_RUN_TIME_SECTION2,
)

_MM_TO_PULSE = (360.0 / 1.8 * 5.0 * 16.0) / (3.1415926535898 * 32)
CALL_TIMEOUT = 5


async def safe_call(coro):
    try:
        return await asyncio.wait_for(coro, timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


async def set_conveyor(box_api, index, enable, speed_mm_s):
    speed = int(math.floor(speed_mm_s * _MM_TO_PULSE)) if enable else 0
    return await safe_call(box_api.SetEMotor(
        portName=DOBOT_PORT, index=index, enable=enable, speed=speed, isQueued=False
    ))


async def stop_both(box_api):
    return await asyncio.gather(
        set_conveyor(box_api, TRAY_CONVEYOR_STP_1, False, 0),
        set_conveyor(box_api, TRAY_CONVEYOR_STP_2, False, 0),
    )


async def start_both(box_api, speed_mm_s):
    return await asyncio.gather(
        set_conveyor(box_api, TRAY_CONVEYOR_STP_1, True, speed_mm_s),
        set_conveyor(box_api, TRAY_CONVEYOR_STP_2, True, speed_mm_s),
    )


async def stopwatch(box_api, speed_mm_s):
    """Enter로 출발 -> Enter로 정지, 구간마다 걸린 시간 기록. input()이 막고 있는 동안에도 벨트는 계속 돎"""
    print(f"[SW] 스톱워치 ({speed_mm_s}mm/s). 지금 설정: section0 {TRAY_CONVEYOR_RUN_TIME_SECTION0}초 / "
          f"section1 {TRAY_CONVEYOR_RUN_TIME_SECTION1}초 / section2 {TRAY_CONVEYOR_RUN_TIME_SECTION2}초")
    laps = []
    while True:
        name = input("\n트레이를 출발 자리에 놓고 Enter (구간 이름 적어도 됨, 예: section1 / q=끝): ").strip()
        if name.lower() == "q":
            break
        r1, r2 = await start_both(box_api, speed_mm_s)
        t0 = time.monotonic()
        print(f"[SW] 출발 (STP1={r1}, STP2={r2}) - 트레이가 자리에 오면 Enter")
        input()
        elapsed = time.monotonic() - t0
        s1, s2 = await stop_both(box_api)
        laps.append((name or f"#{len(laps) + 1}", elapsed))
        print(f"[SW] 정지 (STP1={s1}, STP2={s2}) - 걸린 시간 {elapsed:.2f}초")
    if laps:
        print("\n=== 잰 시간 ===")
        for name, sec in laps:
            print(f"  {name}: {sec:.2f}초")


async def main(arm_api, box_api, run_time, speed_mm_s):
    print(f"[INIT] 연결 시도: {DOBOT_PORT}")
    print(f"[INIT] 연결 결과: {await safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))}")

    try:
        if run_time is None:
            await stopwatch(box_api, speed_mm_s)
            return
        print(f"[CONV] 구동 시작 (STP{TRAY_CONVEYOR_STP_1 + 1}+STP{TRAY_CONVEYOR_STP_2 + 1}, "
              f"{speed_mm_s}mm/s, {run_time}초)")
        r1, r2 = await start_both(box_api, speed_mm_s)
        print(f"[CONV] 시작 결과: STP1={r1}, STP2={r2}")
        await asyncio.sleep(run_time)
    finally:
        s1, s2 = await stop_both(box_api)
        print(f"[CONV] 정지 결과: STP1={s1}, STP2={s2}")
        await safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT))


SECTION_TIMES = {"s0": TRAY_CONVEYOR_RUN_TIME_SECTION0, "s1": TRAY_CONVEYOR_RUN_TIME_SECTION1,
                 "s2": TRAY_CONVEYOR_RUN_TIME_SECTION2}


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a.lower() != "back"]
    reverse = len(argv) != len(sys.argv) - 1
    first = argv[0].lower().replace("section", "s") if argv else "sw"
    if first in SECTION_TIMES:
        run_time = SECTION_TIMES[first]
        print(f"[CONV] {first} 설정 시간 {run_time}초로 구동")
    else:
        run_time = None if first == "sw" else float(argv[0])  # None = 스톱워치
    speed = float(argv[1]) if len(argv) > 1 else TRAY_CONVEYOR_SPEED_MM_S
    if reverse:
        speed = -speed
        print("[CONV] 반대 방향")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # DobotlinkAdapter는 루프가 돌기 전에 만들어야 함 (내부에서 run_until_complete 호출)
    dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
    try:
        loop.run_until_complete(main(dobotlink.MagicianLite, dobotlink.MagicBox, run_time, speed))
    except KeyboardInterrupt:
        # Ctrl+C는 이벤트 루프 밖에서 터져서 main()의 finally(정지 명령)가 안 돎 - 여기서 직접 정지
        s1, s2 = loop.run_until_complete(stop_both(dobotlink.MagicBox))
        print(f"[CONV] Ctrl+C - 정지 결과: STP1={s1}, STP2={s2}")
        loop.run_until_complete(safe_call(dobotlink.MagicianLite.DisconnectDobot(portName=DOBOT_PORT)))
