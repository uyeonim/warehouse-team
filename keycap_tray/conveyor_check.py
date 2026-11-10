# -*- coding: utf-8 -*-
"""
컨베이어 확인 (테이블 다시 조립한 뒤 - 어느 매직박스의 어느 구멍(STP1/STP2)이 어느 벨트인지, 방향은 맞는지)

꽂혀 있는 매직박스마다 STP1(index 0), STP2(index 1)를 하나씩 "+ 방향"으로 짧게(기본 2초, 30mm/s) 돌림.
어느 벨트가 어느 쪽으로 움직였는지 입력하면, 끝에 지금 코드 설정과 비교한 표를 보여줌. 설정 파일은 고치지 않음.

지금 코드 설정 (+ 방향으로 돌렸을 때 원래 흐름 방향이어야 하는 것):
  보드 팔 매직박스   STP1 + STP2  트레이 벨트 2개 (둘을 동시에 같은 속도로, + 방향)   board_config.py
  키캡 세트1 매직박스  STP1        키캡 벨트 세트1 (- 방향 = conveyor_reverse True)    dobot_control.py SETS set1
  키캡 세트2 매직박스  STP2        키캡 벨트 세트2 (+ 방향)                            dobot_control.py SETS set2
  (2026-11-11 테이블 재조립 뒤 확인값)

실행 (저장소 폴더에서):  .\venv\Scripts\python.exe keycap_tray\conveyor_check.py [초] [mm/s]
  - 창고 프로그램·DobotLab 끄기. 벨트 위 키캡·트레이는 치우거나 움직여도 되는 상태로
"""

import asyncio
import math
import sys

from DobotRPC import RPCClient, DobotlinkAdapter

from identify_arms import PLACES, call, configured_serials
from usb_serial import list_ports

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
_MM_TO_PULSE = (360.0 / 1.8 * 5.0 * 16.0) / (math.pi * 32)  # dobot_control.mm_per_sec_to_pulses 와 같음

BELTS = {"t": "트레이 벨트", "1": "키캡 벨트 세트1", "2": "키캡 벨트 세트2", "n": "안 움직임"}
# 지금 코드가 기대하는 것: (자리 키, STP index) -> (벨트 키, + 방향이 원래 흐름이면 "f" / 반대면 "r")
EXPECTED = {("b", 0): ("t", "f"), ("b", 1): ("t", "f"), ("1", 0): ("1", "r"), ("2", 1): ("2", "f")}  # 2026-11-11 재조립 뒤


async def run_one(arm, box, port, index):
    res = await call(arm.ConnectDobot(portName=port))
    if isinstance(res, str) and (res.startswith("ERROR") or res.startswith("TIMEOUT")):
        return f"연결 실패: {res}"
    try:
        start = await call(box.SetEMotor(portName=port, index=index, enable=True,
                                         speed=int(SPEED * _MM_TO_PULSE), isQueued=False))
        await asyncio.sleep(SECONDS)
        return f"시작 {start}"
    finally:
        stop = await call(box.SetEMotor(portName=port, index=index, enable=False, speed=0, isQueued=False))
        print(f"  정지 {stop}")
        await call(arm.DisconnectDobot(portName=port))


def main():
    ports = list_ports()
    if not ports:
        print("꽂혀 있는 Dobot이 없음 - 매직박스 전원·USB 확인")
        return
    place_of = {s: k for k, s in configured_serials().items() if s}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    link = DobotlinkAdapter(RPCClient(), is_sync=False)
    arm, box = getattr(link, "MagicianLite"), getattr(link, "MagicBox")
    print(f"STP 하나씩 + 방향으로 {SECONDS}초, {SPEED}mm/s (약 {SECONDS * SPEED:.0f}mm)\n")

    seen = {}  # (자리 키, index) -> (벨트 키, 방향)
    try:
        for serial, port in sorted(ports.items(), key=lambda kv: "b12t".find(place_of.get(kv[0], "z")) % 9):
            place = place_of.get(serial, "?")
            for index in (0, 1):
                label = f"{PLACES.get(place, '설정에 없음')} 매직박스 ({serial}, {port}) STP{index + 1}"
                if input(f"=== {label}\n  Enter = 돌리기, s = 건너뛰기: ").strip().lower() == "s":
                    continue
                while True:
                    print("  " + loop.run_until_complete(run_one(arm, box, port, index)))
                    belt = input("  움직인 벨트? t=트레이 1=키캡세트1 2=키캡세트2 n=안 움직임 r=다시: ").strip().lower()
                    if belt != "r":
                        break
                direction = ""
                if belt in ("t", "1", "2"):
                    direction = input("  방향? f=원래 흐르는 방향 b=반대: ").strip().lower()
                    direction = "f" if direction == "f" else "r"
                seen[(place, index)] = (belt if belt in BELTS else "?", direction)
                print()
    except (KeyboardInterrupt, EOFError):
        print("\n중단")

    print("\n===== 결과 (+ 방향으로 돌렸을 때) =====")
    for (place, index), (belt, direction) in seen.items():
        want = EXPECTED.get((place, index))
        got = BELTS.get(belt, "모름") + ({"f": " 정방향", "r": " 반대"}.get(direction, ""))
        want_txt = (BELTS[want[0]] + (" 정방향" if want[1] == "f" else " 반대")) if want else "코드에서 안 씀"
        ok = (want is None and belt == "n") or (want is not None and (belt, direction) == want)
        print(f"  {PLACES.get(place, '?')} STP{index + 1}: 코드 기대 {want_txt:<18} 실제 {got:<18} {'OK' if ok else '<- 다름'}")


if __name__ == "__main__":
    main()
