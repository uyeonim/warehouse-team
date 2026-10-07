# -*- coding: utf-8 -*-
"""
트레이 컨베이어 단독 구동 테스트 - 로봇암은 안 움직임.
메인과 같은 방식으로 STP1 + STP2(벨트 2개)를 동시에 켜고, 지정 시간 뒤 정지.

사용: python board_conveyor_test.py [초] [속도mm/s]
  python board_conveyor_test.py          -> section1 시간(TRAY_CONVEYOR_RUN_TIME_SECTION1)만큼
  python board_conveyor_test.py 5        -> 5초
  python board_conveyor_test.py 5 30     -> 5초, 30mm/s

!! 메인(main_board_robot_arm.py) 끄고 실행할 것 - 같은 포트(Magic Box)를 씀
"""

import asyncio
import math
import sys

from DobotRPC import RPCClient, DobotlinkAdapter

from board_config import (
    DOBOT_PORT, TRAY_CONVEYOR_STP_1, TRAY_CONVEYOR_STP_2,
    TRAY_CONVEYOR_SPEED_MM_S, TRAY_CONVEYOR_RUN_TIME_SECTION1,
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


async def main(arm_api, box_api, run_time, speed_mm_s):
    print(f"[INIT] 연결 시도: {DOBOT_PORT}")
    print(f"[INIT] 연결 결과: {await safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT))}")

    try:
        print(f"[CONV] 구동 시작 (STP{TRAY_CONVEYOR_STP_1 + 1}+STP{TRAY_CONVEYOR_STP_2 + 1}, "
              f"{speed_mm_s}mm/s, {run_time}초)")
        r1, r2 = await asyncio.gather(
            set_conveyor(box_api, TRAY_CONVEYOR_STP_1, True, speed_mm_s),
            set_conveyor(box_api, TRAY_CONVEYOR_STP_2, True, speed_mm_s),
        )
        print(f"[CONV] 시작 결과: STP1={r1}, STP2={r2}")
        await asyncio.sleep(run_time)
    finally:
        s1, s2 = await stop_both(box_api)
        print(f"[CONV] 정지 결과: STP1={s1}, STP2={s2}")
        await safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT))


if __name__ == "__main__":
    run_time = float(sys.argv[1]) if len(sys.argv) > 1 else TRAY_CONVEYOR_RUN_TIME_SECTION1
    speed = float(sys.argv[2]) if len(sys.argv) > 2 else TRAY_CONVEYOR_SPEED_MM_S
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
